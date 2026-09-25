"""LangGraph incident-response workflow.

intake -> Send x4 (parallel evidence collection) -> diagnose
diagnose -(low confidence, round 1)-> Send(extended logs) -> diagnose
diagnose -(accepted)-> propose -> approval [interrupt for medium/high risk] -> execute -> verify
diagnose -(still unclear)-> escalate
verify | escalate | rejected -> postmortem -> END
"""

from __future__ import annotations

import operator
import os
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from incident_copilot import diagnostics, simulator
from incident_copilot.postmortem import write_postmortem

NEEDS_APPROVAL = {"medium", "high"}


class State(TypedDict, total=False):
    incident_id: str
    incident: dict
    round: int
    evidence: Annotated[list[dict], operator.add]
    diagnosis: dict
    proposal: dict
    approval: dict
    execution: dict
    status: str
    audit: Annotated[list[str], operator.add]
    postmortem: str


class CollectInput(TypedDict):
    source: str
    incident: dict
    extended: bool


def intake(state: State) -> dict:
    incident = simulator.load(state["incident_id"])
    return {
        "incident": incident,
        "round": 1,
        "status": "investigating",
        "audit": [f"intake: {incident['alert']['name']} on {incident['service']}"],
    }


def fan_out(state: State) -> list[Send]:
    return [
        Send("collect", {"source": s, "incident": state["incident"], "extended": False})
        for s in diagnostics.COLLECTORS
    ]


def collect(payload: CollectInput) -> dict:
    source = payload["source"]
    if source == "logs":
        found = diagnostics.collect_logs(payload["incident"], extended=payload["extended"])
    else:
        found = diagnostics.COLLECTORS[source](payload["incident"])
    label = "logs (extended window)" if payload["extended"] else source
    return {"evidence": found, "audit": [f"collect: {label} -> {len(found)} evidence item(s)"]}


def diagnose(state: State) -> dict:
    d = diagnostics.score(state["evidence"])
    verdict = "accepted" if d["accepted"] else "insufficient"
    return {
        "diagnosis": d,
        "audit": [
            f"diagnose round {state['round']}: top={d['top_cause']} score={d['top_score']} "
            f"confidence={d['confidence']} -> {verdict}"
        ],
    }


def route_after_diagnose(state: State) -> Any:
    if state["diagnosis"]["accepted"]:
        return "propose"
    if state["round"] == 1:
        return "deep_dive"
    return "escalate"


def deep_dive(state: State) -> dict:
    return {"round": 2, "audit": ["deep_dive: widening the log window"]}


def fan_out_deep(state: State) -> list[Send]:
    return [Send("collect", {"source": "logs", "incident": state["incident"], "extended": True})]


def propose(state: State) -> dict:
    cause = state["diagnosis"]["top_cause"]
    action, risk = diagnostics.RUNBOOK[cause]
    proposal = {
        "cause": cause,
        "action": action,
        "risk": risk,
        "confidence": state["diagnosis"]["confidence"],
    }
    return {"proposal": proposal, "audit": [f"propose: {action} (risk={risk}) for {cause}"]}


def approval(state: State) -> dict:
    p = state["proposal"]
    if p["risk"] not in NEEDS_APPROVAL:
        decision = {"approved": True, "approver": "policy:auto-approve-low-risk", "note": ""}
    else:
        # Pauses the run; the checkpoint survives process restarts until someone resumes it.
        decision = interrupt(
            {
                "incident": state["incident_id"],
                "service": state["incident"]["service"],
                "question": f"Approve {p['action']} ({p['risk']} risk)?",
                "proposal": p,
                "evidence": [e["signal"] for e in state["diagnosis"]["supporting"]],
            }
        )
    verdict = "approved" if decision.get("approved") else "rejected"
    return {
        "approval": decision,
        "status": "approved" if decision.get("approved") else "rejected",
        "audit": [f"approval: {verdict} by {decision.get('approver', 'unknown')}"],
    }


def execute(state: State) -> dict:
    result = simulator.apply(state["incident"], state["proposal"]["action"])
    return {"execution": result, "audit": [f"execute: {result['action']}"]}


def verify(state: State) -> dict:
    ok = state["execution"]["recovered"]
    status = "resolved" if ok else "escalated_after_failed_remediation"
    return {"status": status, "audit": [f"verify: {'recovered' if ok else 'not recovered'}"]}


def escalate(state: State) -> dict:
    return {
        "status": "escalated",
        "proposal": {"cause": None, "action": "escalate", "risk": "none", "confidence": 0.0},
        "audit": ["escalate: evidence inconclusive, paging on-call with collected evidence"],
    }


def postmortem(state: State) -> dict:
    return {"postmortem": write_postmortem(state, use_llm=os.getenv("INCIDENT_COPILOT_LLM"))}


def build_graph(checkpointer=None):
    g = StateGraph(State)
    for name, fn in [
        ("intake", intake), ("collect", collect), ("diagnose", diagnose),
        ("deep_dive", deep_dive), ("propose", propose), ("approval", approval),
        ("execute", execute), ("verify", verify), ("escalate", escalate),
        ("postmortem", postmortem),
    ]:  # fmt: skip
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", fan_out, ["collect"])
    g.add_edge("collect", "diagnose")
    g.add_conditional_edges("diagnose", route_after_diagnose, ["propose", "deep_dive", "escalate"])
    g.add_conditional_edges("deep_dive", fan_out_deep, ["collect"])
    g.add_edge("propose", "approval")
    g.add_conditional_edges(
        "approval", lambda s: "execute" if s["approval"].get("approved") else "postmortem",
        ["execute", "postmortem"],
    )  # fmt: skip
    g.add_edge("execute", "verify")
    g.add_edge("verify", "postmortem")
    g.add_edge("escalate", "postmortem")
    g.add_edge("postmortem", END)
    return g.compile(checkpointer=checkpointer)
