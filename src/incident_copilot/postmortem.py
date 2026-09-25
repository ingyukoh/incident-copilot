"""Postmortem writer. Facts come from graph state; an LLM may only rephrase the summary."""

from __future__ import annotations


def _facts(state: dict) -> dict:
    inc, d = state["incident"], state.get("diagnosis", {})
    p, a, x = state.get("proposal", {}), state.get("approval"), state.get("execution")
    return {"incident": inc, "diagnosis": d, "proposal": p, "approval": a, "execution": x}


def _template_summary(state: dict) -> str:
    f = _facts(state)
    inc, p = f["incident"], f["proposal"]
    status = state.get("status", "unknown")
    if p.get("action") == "escalate":
        return (
            f"{inc['alert']['name']} on {inc['service']}: evidence stayed inconclusive after a "
            f"widened log search, so the incident was escalated to on-call without any change."
        )
    return (
        f"{inc['alert']['name']} on {inc['service']} was attributed to {p['cause']} "
        f"(confidence {p['confidence']:.2f}). Proposed {p['action']} ({p['risk']} risk); "
        f"final status: {status}."
    )


def _llm_summary(state: dict, provider: str) -> str:
    if provider != "anthropic":
        raise ValueError(f"unsupported provider {provider}")
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(model="claude-sonnet-5", temperature=0, max_tokens=300)
    prompt = (
        "Rewrite this incident summary as two plain sentences for an engineering postmortem. "
        "Do not add facts, causes, or numbers that are not in it.\n\n" + _template_summary(state)
    )
    return str(model.invoke(prompt).content).strip()


def write_postmortem(state: dict, use_llm: str | None = None) -> str:
    f = _facts(state)
    inc, d = f["incident"], f["diagnosis"]
    summary = _llm_summary(state, use_llm) if use_llm else _template_summary(state)
    lines = [
        f"# Postmortem {inc['id']}: {inc['title']}",
        "",
        f"**Status:** {state.get('status')}  ",
        f"**Alert:** {inc['alert']['name']} - {inc['alert']['summary']}",
        "",
        "## Summary",
        summary,
        "",
        "## Hypotheses",
        "| Cause | Score |",
        "|---|---:|",
        *[f"| {h['cause']} | {h['score']} |" for h in d.get("ranking", [])],
        "",
        "## Evidence",
        "| Source | Supports | Weight | Signal | Ref |",
        "|---|---|---:|---|---|",
        *[
            f"| {e['source']} | {e['cause']} | {e['weight']} | {e['signal']} | `{e['ref']}` |"
            for e in state.get("evidence", [])
        ],
        "",
        "## Decision",
    ]
    if f["approval"]:
        a = f["approval"]
        lines.append(
            f"- Approval: {'approved' if a.get('approved') else 'rejected'} by "
            f"{a.get('approver')}{' - ' + a['note'] if a.get('note') else ''}"
        )
    if f["execution"]:
        lines.append(
            f"- Executed `{f['execution']['action']}`: {f['execution']['post_alert_state']}"
        )
    lines += ["", "## Timeline (graph audit log)", *[f"1. {a}" for a in state.get("audit", [])]]
    return "\n".join(lines) + "\n"
