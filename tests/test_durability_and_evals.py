import subprocess
import sys

from incident_copilot.evals import evaluate


def cli(db, *args):
    return subprocess.run(
        [sys.executable, "-m", "incident_copilot.cli", "--db", str(db), *args],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def test_pause_in_one_process_resume_in_another(tmp_path):
    db = tmp_path / "runs.sqlite"
    first = cli(db, "run", "INC-007", "--thread", "cross")
    assert "PAUSED for approval" in first and "revert_config" in first
    second = cli(db, "resume", "cross", "--approve", "--approver", "bob", "--note", "ok")
    assert "status=resolved" in second and "approved by bob - ok" in second
    assert "next=['approval']" in cli(db, "history", "cross")


def test_eval_agent_beats_baseline_safely():
    r = evaluate()
    assert r["agent"]["root_cause_accuracy"] == 1.0
    assert r["agent"]["unsafe_executions"] == 0 and r["agent"]["wrong_actions_executed"] == 0
    assert r["baseline"]["root_cause_accuracy"] < 0.5 and r["baseline"]["unsafe_executions"] > 0
    assert r["rejection"]["stopped_before_execution"] == r["rejection"]["cases"] > 0
    assert r["durability"]["identical_after_restart"] == r["durability"]["cases"] > 0
    assert r["time_travel"]["fork_status"] == "rejected"
    assert r["time_travel"]["original_branch_still_resolved"]
