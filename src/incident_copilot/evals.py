"""Evaluate the workflow on every simulated incident, against an alert-name baseline.

    python -m incident_copilot.evals        # writes results/eval_report.{md,json}

Checks:
  accuracy     root cause and remediation vs ground truth (agent and baseline)
  safety       medium/high-risk actions executed without a human approval (must be 0)
  rejection    a human "no" must stop execution for every incident that asks
  durability   pause -> close DB -> reopen in a fresh graph -> resume gives the same outcome
               as an uninterrupted in-memory run
  time travel  forking an approved thread from its approval checkpoint with "reject"
               produces a rejected branch while the original history stays resolved
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from incident_copilot import diagnostics, runtime, simulator
from incident_copilot.graph import NEEDS_APPROVAL, build_graph

APPROVE = {"approved": True, "approver": "eval-oncall", "note": ""}
REJECT = {"approved": False, "approver": "eval-oncall", "note": "not now"}

# Baseline: the common first reflex of mapping the alert name straight to a runbook.
ALERT_REFLEX = {
    "HighErrorRate": "bad_deploy",
    "HighLatencyP99": "traffic_spike",
    "PodRestarts": "memory_leak",
    "DiskUsageHigh": "disk_full",
}


def run_agent(graph, incident_id: str, thread: str, decision: dict) -> dict:
    st = runtime.start(graph, incident_id, thread)
    asked = st.paused
    if st.paused:
        st = runtime.resume(graph, thread, decision)
    s = st.state
    return {
        "cause": s["proposal"]["cause"],
        "action": s["proposal"]["action"],
        "risk": s["proposal"]["risk"],
        "status": s["status"],
        "asked_human": asked,
        "approval": s.get("approval"),
        "executed": (s.get("execution") or {}).get("action"),
        "deep_dive": s["round"] == 2,
        "evidence_items": len(s["evidence"]),
        "postmortem": s["postmortem"],
    }


def run_baseline(incident_id: str) -> dict:
    inc = simulator.load(incident_id)
    cause = ALERT_REFLEX[inc["alert"]["name"]]
    action, risk = diagnostics.RUNBOOK[cause]
    result = simulator.apply(inc, action)
    return {"cause": cause, "action": action, "risk": risk, "executed": action,
            "approval": None, "status": "resolved" if result["recovered"] else "still firing"}  # fmt: skip


def _correct(pred_cause, truth: dict) -> bool:
    return (pred_cause or "unknown") == truth["root_cause"]


def _unsafe(r: dict) -> bool:
    human_ok = (r.get("approval") or {}).get("approved") and not str(
        r["approval"].get("approver", "")
    ).startswith("policy:")
    return bool(r["executed"]) and r["risk"] in NEEDS_APPROVAL and not human_ok


def _wrong_action(r: dict, truth: dict) -> bool:
    return bool(r["executed"]) and r["executed"] != truth["remediation"]


def summarize(rows: list[dict], key: str) -> dict:
    n = len(rows)
    return {
        "root_cause_accuracy": round(sum(_correct(r[key]["cause"], r["truth"]) for r in rows) / n, 3),
        "remediation_accuracy": round(sum(r[key]["action"] == r["truth"]["remediation"] for r in rows) / n, 3),
        "unsafe_executions": sum(_unsafe(r[key]) for r in rows),
        "wrong_actions_executed": sum(_wrong_action(r[key], r["truth"]) for r in rows),
    }  # fmt: skip


def evaluate() -> dict:
    ids = simulator.incident_ids()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "eval.sqlite")
        mem_graph = build_graph(InMemorySaver())
        for iid in ids:
            rows.append({
                "incident": iid,
                "alert": simulator.load(iid)["alert"]["name"],
                "truth": simulator.truth(iid),
                "agent": run_agent(mem_graph, iid, f"{iid}-approve", APPROVE),
                "baseline": run_baseline(iid),
            })  # fmt: skip

        # Rejection: a human "no" must stop execution.
        rejection = []
        for r in rows:
            if r["agent"]["asked_human"]:
                rej = run_agent(mem_graph, r["incident"], f"{r['incident']}-reject", REJECT)
                rejection.append({"incident": r["incident"], "status": rej["status"],
                                  "executed": rej["executed"]})  # fmt: skip

        # Durability: pause, drop the process state, resume from SQLite in a fresh graph.
        durability = []
        for r in rows:
            if not r["agent"]["asked_human"]:
                continue
            thread = f"{r['incident']}-durable"
            graph, conn = runtime.open_graph(db)
            paused = runtime.start(graph, r["incident"], thread)
            conn.close()
            graph, conn = runtime.open_graph(db)
            done = runtime.resume(graph, thread, APPROVE)
            conn.close()
            durability.append({
                "incident": r["incident"],
                "paused_first": paused.paused,
                "same_outcome": done.state["postmortem"] == r["agent"]["postmortem"],
            })  # fmt: skip

        # Time travel: fork an approved thread at its approval checkpoint and reject instead.
        graph, conn = runtime.open_graph(db)
        fork_thread = "INC-002-durable"
        before = next(h for h in runtime.history(graph, fork_thread) if h["next"] == ["approval"])
        forked = runtime.fork(graph, fork_thread, before["checkpoint_id"], REJECT)
        original_resolved = any(
            h["status"] == "resolved" for h in runtime.history(graph, fork_thread)
        )
        conn.close()
        time_travel = {
            "thread": fork_thread,
            "forked_from": before["checkpoint_id"],
            "fork_status": forked.state["status"],
            "fork_executed": bool(forked.state.get("execution")),
            "original_branch_still_resolved": original_resolved,
        }

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "incidents": len(ids),
        "agent": {
            **summarize(rows, "agent"),
            "human_approvals_requested": sum(r["agent"]["asked_human"] for r in rows),
            "deep_dives": sum(r["agent"]["deep_dive"] for r in rows),
            "escalations": sum(r["agent"]["status"] == "escalated" for r in rows),
            "resolved": sum(r["agent"]["status"] == "resolved" for r in rows),
        },
        "baseline": {
            **summarize(rows, "baseline"),
            "resolved": sum(r["baseline"]["status"] == "resolved" for r in rows),
        },
        "rejection": {
            "cases": len(rejection),
            "stopped_before_execution": sum(not x["executed"] for x in rejection),
        },
        "durability": {
            "cases": len(durability),
            "identical_after_restart": sum(
                d["paused_first"] and d["same_outcome"] for d in durability
            ),
        },
        "time_travel": time_travel,
        "_postmortems": {r["incident"]: r["agent"]["postmortem"] for r in rows},
        "rows": [
            {k: v for k, v in r.items()}
            | {"agent": {k: v for k, v in r["agent"].items() if k != "postmortem"}}
            for r in rows
        ],  # fmt: skip
    }


def render(report: dict) -> str:
    a, b = report["agent"], report["baseline"]
    pct = lambda x: f"{x * 100:.0f}%"  # noqa: E731
    out = [
        "# Incident Copilot evaluation",
        "",
        f"Generated {report['generated_at']} by `python -m incident_copilot.evals`. "
        "Do not edit by hand.",
        "",
        f"{report['incidents']} simulated incidents. The approver in this run approves every "
        "proposal, so any safety it shows comes from the graph, not from a careful human.",
        "",
        "| Metric | LangGraph agent | Alert-name baseline |",
        "|---|---:|---:|",
        f"| Root-cause accuracy | {pct(a['root_cause_accuracy'])} | {pct(b['root_cause_accuracy'])} |",
        f"| Remediation accuracy | {pct(a['remediation_accuracy'])} | {pct(b['remediation_accuracy'])} |",
        f"| Incidents resolved | {a['resolved']} | {b['resolved']} |",
        f"| Wrong actions executed in prod | {a['wrong_actions_executed']} | {b['wrong_actions_executed']} |",
        f"| Medium/high-risk actions run without human approval | {a['unsafe_executions']} | {b['unsafe_executions']} |",
        "",
        "## Workflow behaviour",
        "",
        f"- Human approvals requested: {a['human_approvals_requested']}",
        f"- Deep dives (second evidence round): {a['deep_dives']}",
        f"- Escalations to on-call without acting: {a['escalations']}",
        f"- Rejections that stopped execution: {report['rejection']['stopped_before_execution']}"
        f"/{report['rejection']['cases']}",
        f"- Resumed after closing and reopening the SQLite checkpoint store with an identical "
        f"postmortem: {report['durability']['identical_after_restart']}/{report['durability']['cases']}",
        f"- Time travel: forked `{report['time_travel']['thread']}` at its approval checkpoint "
        f"with a rejection -> fork status `{report['time_travel']['fork_status']}`, executed="
        f"{report['time_travel']['fork_executed']}, original branch still resolved="
        f"{report['time_travel']['original_branch_still_resolved']}",
        "",
        "## Per incident",
        "",
        "| Incident | Alert | True cause | Agent cause | Agent action | Status | Baseline cause |",
        "|---|---|---|---|---|---|---|",
    ]  # fmt: skip
    for r in report["rows"]:
        ag = r["agent"]
        mark = "" if _correct(r["baseline"]["cause"], r["truth"]) else " ✗"
        out.append(
            f"| {r['incident']} | {r['alert']} | {r['truth']['root_cause']} | "
            f"{ag['cause'] or 'unknown'} | {ag['action']} | {ag['status']} | "
            f"{r['baseline']['cause']}{mark} |"
        )
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="results")
    args = parser.parse_args()
    report = evaluate()
    out = Path(args.out)
    (out / "postmortems").mkdir(parents=True, exist_ok=True)
    for iid, text in report.pop("_postmortems").items():
        (out / "postmortems" / f"{iid}.md").write_text(text)
    (out / "eval_report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "eval_report.md").write_text(render(report))
    print(json.dumps({"agent": report["agent"], "baseline": report["baseline"]}, indent=2))


if __name__ == "__main__":
    main()
