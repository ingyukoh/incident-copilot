"""Evidence collectors and the hypothesis scorer.

Each collector reads one signal source and emits weighted, cited evidence for (or against)
root-cause hypotheses from a fixed taxonomy. The scorer combines them; nothing here knows
the ground truth.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CAUSES = [
    "bad_deploy", "config_error", "memory_leak", "db_connection_exhaustion",
    "dependency_outage", "disk_full", "cert_expired", "traffic_spike",
]  # fmt: skip

# cause -> (action, risk). Medium and high risk actions require human approval.
RUNBOOK = {
    "bad_deploy": ("rollback_deploy", "high"),
    "config_error": ("revert_config", "high"),
    "memory_leak": ("restart_pods", "medium"),
    "db_connection_exhaustion": ("increase_db_pool", "medium"),
    "cert_expired": ("renew_certificate", "medium"),
    "dependency_outage": ("enable_circuit_breaker", "low"),
    "traffic_spike": ("scale_out", "low"),
    "disk_full": ("rotate_logs", "low"),
}

ACCEPT_SCORE = 3.0
ACCEPT_CONFIDENCE = 0.6
DEPLOY_WINDOW_MIN = 30

LOG_PATTERNS = [
    ("db_connection_exhaustion", 3.0, r"connection is not available|pool exhausted|waiting=\d{2,}"),
    ("dependency_outage", 3.0, r"upstream \S+ returned 5\d\d|retry budget exhausted"),
    ("memory_leak", 3.0, r"OutOfMemoryError|OOMKilled"),
    ("memory_leak", 1.0, r"GC pause|GC overhead"),
    ("disk_full", 3.0, r"No space left on device"),
    ("cert_expired", 3.0, r"certificate has expired|x509"),
    ("config_error", 3.0, r"invalid configuration|missing required env"),
    ("bad_deploy", 2.0, r"TypeError|NullPointerException|ReferenceError|AttributeError|KeyError"),
    ("traffic_spike", 1.0, r"queue depth \d{3,}"),
]


@dataclass(frozen=True)
class Evidence:
    source: str
    cause: str
    weight: float
    signal: str
    ref: str

    def key(self) -> tuple[str, str]:
        return (self.source, self.cause)


def collect_logs(incident: dict, extended: bool = False) -> list[dict]:
    lines = incident["logs"] + (incident.get("extended_logs", []) if extended else [])
    source = "logs_extended" if extended else "logs"
    out = []
    for cause, weight, pattern in LOG_PATTERNS:
        hits = [i for i, line in enumerate(lines) if re.search(pattern, line, re.IGNORECASE)]
        if hits:
            sample = lines[hits[0]]
            signal = f"{len(hits)} log line(s) match '{pattern.split('|')[0]}': {sample[:90]}"
            out.append(Evidence(source, cause, weight, signal, f"{source}[{hits[0]}]"))
    return [asdict(e) for e in out]


def _increasing_fraction(series: list[float]) -> float:
    steps = list(zip(series, series[1:], strict=False))
    return sum(b > a for a, b in steps) / len(steps) if steps else 0.0


def collect_metrics(incident: dict) -> list[dict]:
    m, out = incident["metrics"], []
    if mem := m.get("memory_mb"):
        peak = max(mem)
        upto = mem[: mem.index(peak) + 1]
        if peak / mem[0] >= 1.5 and _increasing_fraction(upto) >= 0.8:
            out.append(Evidence("metrics", "memory_leak", 2.5,
                                f"memory grew {mem[0]}->{peak} MB, rising in every interval",
                                "metrics.memory_mb"))  # fmt: skip
    if (conns := m.get("db_connections")) and (cap := m.get("db_pool_max")):
        if conns[-1] >= 0.95 * cap:
            out.append(Evidence("metrics", "db_connection_exhaustion", 2.0,
                                f"db connections {conns[-1]}/{cap} (pool saturated)",
                                "metrics.db_connections"))  # fmt: skip
    if (disk := m.get("disk_used_pct")) and disk[-1] >= 95:
        out.append(Evidence("metrics", "disk_full", 2.5, f"disk at {disk[-1]}%",
                            "metrics.disk_used_pct"))  # fmt: skip
    if (rps := m.get("rps")) and rps[-1] / rps[0] >= 2.5:
        out.append(Evidence("metrics", "traffic_spike", 2.5,
                            f"request rate {rps[0]}->{rps[-1]} rps ({rps[-1] / rps[0]:.1f}x)",
                            "metrics.rps"))  # fmt: skip
        if (cpu := m.get("cpu_pct")) and cpu[-1] >= 85:
            out.append(Evidence("metrics", "traffic_spike", 0.5, f"cpu at {cpu[-1]}%",
                                "metrics.cpu_pct"))  # fmt: skip
    return [asdict(e) for e in out]


def collect_deploys(incident: dict) -> list[dict]:
    onset, out = incident["onset_minutes_before_alert"], []
    for d in incident["deploys"]:
        age = d["minutes_before_alert"]
        if age > DEPLOY_WINDOW_MIN:
            continue
        ref = f"deploys[{d['version']}]"
        if age < onset:
            # Symptoms started before this deploy: evidence against the deploy.
            out.append(Evidence("deploys", "bad_deploy", -1.5,
                                f"{d['version']} deployed {age}m before alert, after onset "
                                f"({onset}m before)", ref))  # fmt: skip
            continue
        out.append(Evidence("deploys", "bad_deploy", 1.5 if d["config_changed"] else 2.0,
                            f"{d['version']} deployed {age}m before alert, "
                            f"{age - onset}m before onset", ref))  # fmt: skip
        if d["config_changed"]:
            out.append(Evidence("deploys", "config_error", 1.5,
                                f"{d['version']} changed configuration", ref))  # fmt: skip
    return [asdict(e) for e in out]


def collect_dependencies(incident: dict) -> list[dict]:
    return [
        asdict(Evidence("dependencies", "dependency_outage", 2.0,
                        f"upstream {name} error rate {rate:.0%}", f"dependencies.{name}"))
        for name, rate in incident["dependencies"].items()
        if rate >= 0.2
    ]  # fmt: skip


COLLECTORS = {
    "logs": collect_logs,
    "metrics": collect_metrics,
    "deploys": collect_deploys,
    "dependencies": collect_dependencies,
}


def score(evidence: list[dict]) -> dict:
    """Combine evidence; the strongest item per (source, cause) counts once."""
    best: dict[tuple[str, str], dict] = {}
    for e in evidence:
        source = "logs" if e["source"] == "logs_extended" else e["source"]
        k = (source, e["cause"])
        if k not in best or abs(e["weight"]) > abs(best[k]["weight"]):
            best[k] = e
    totals = {c: 0.0 for c in CAUSES}
    for (_, cause), e in best.items():
        totals[cause] += e["weight"]
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    positive = sum(v for v in totals.values() if v > 0)
    top_cause, top_score = ranked[0]
    confidence = top_score / positive if positive else 0.0
    return {
        "ranking": [{"cause": c, "score": round(s, 2)} for c, s in ranked if s != 0],
        "top_cause": top_cause if top_score > 0 else None,
        "top_score": round(top_score, 2),
        "confidence": round(confidence, 2),
        "accepted": top_score >= ACCEPT_SCORE and confidence >= ACCEPT_CONFIDENCE,
        "supporting": [e for e in best.values() if e["cause"] == top_cause],
    }
