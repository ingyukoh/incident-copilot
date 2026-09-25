from langgraph.checkpoint.memory import InMemorySaver

from incident_copilot import runtime
from incident_copilot.graph import build_graph

APPROVE = {"approved": True, "approver": "alice", "note": ""}
REJECT = {"approved": False, "approver": "alice", "note": "no"}


def graph():
    return build_graph(InMemorySaver())


def test_parallel_fan_out_runs_all_collectors_in_one_step():
    g = graph()
    steps = []
    runtime.start(g, "INC-005", "fan", on_update=lambda u: steps.append(list(u)))
    collect_steps = [s for s in steps if s == ["collect"]]
    assert len(collect_steps) == 4


def test_high_risk_pauses_with_question_and_evidence():
    st = runtime.start(graph(), "INC-001", "p")
    assert st.paused
    assert st.question["proposal"]["action"] == "rollback_deploy"
    assert st.question["evidence"]


def test_low_risk_is_auto_approved_by_policy():
    st = runtime.start(graph(), "INC-008", "low")
    assert not st.paused and st.state["status"] == "resolved"
    assert st.state["approval"]["approver"] == "policy:auto-approve-low-risk"


def test_rejection_stops_execution():
    g = graph()
    runtime.start(g, "INC-002", "r")
    st = runtime.resume(g, "r", REJECT)
    assert st.state["status"] == "rejected" and "execution" not in st.state
    assert "rejected by alice" in st.state["postmortem"]


def test_inconclusive_evidence_escalates_without_action():
    st = runtime.start(graph(), "INC-010", "esc")
    assert st.state["status"] == "escalated" and "execution" not in st.state
    assert st.state["round"] == 2


def test_time_travel_fork_changes_decision_and_keeps_original():
    g = graph()
    runtime.start(g, "INC-004", "tt")
    runtime.resume(g, "tt", APPROVE)
    cp = next(h for h in runtime.history(g, "tt") if h["next"] == ["approval"])
    forked = runtime.fork(g, "tt", cp["checkpoint_id"], REJECT)
    assert forked.state["status"] == "rejected"
    assert any(h["status"] == "resolved" for h in runtime.history(g, "tt"))
