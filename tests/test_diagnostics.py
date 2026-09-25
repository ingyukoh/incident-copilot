from incident_copilot import diagnostics as d
from incident_copilot import simulator


def test_agent_view_hides_ground_truth():
    for iid in simulator.incident_ids():
        assert "truth" not in simulator.load(iid)


def test_deploy_after_onset_counts_against_bad_deploy():
    ev = d.collect_deploys(simulator.load("INC-003"))
    assert [(e["cause"], e["weight"]) for e in ev] == [("bad_deploy", -1.5)]


def test_old_deploy_outside_window_is_ignored():
    assert d.collect_deploys(simulator.load("INC-002")) == []


def test_config_change_supports_config_error():
    causes = {e["cause"] for e in d.collect_deploys(simulator.load("INC-007"))}
    assert causes == {"bad_deploy", "config_error"}


def test_extended_logs_reveal_cert_expiry():
    inc = simulator.load("INC-006")
    assert d.collect_logs(inc) == []
    assert [e["cause"] for e in d.collect_logs(inc, extended=True)] == ["cert_expired"]


def test_memory_leak_detected_from_trend_not_single_point():
    assert [e["cause"] for e in d.collect_metrics(simulator.load("INC-009"))] == ["memory_leak"]
    assert d.collect_metrics(simulator.load("INC-007")) == []  # memory dropped: crash, not leak


def test_score_dedupes_repeated_source_and_needs_threshold():
    ev = d.collect_logs(simulator.load("INC-006"), extended=True) * 3
    result = d.score(ev)
    assert result["top_score"] == 3.0 and result["accepted"]
    assert not d.score([])["accepted"]
