"""Simulated environment: loads incidents and applies remediations.

The environment (not the agent) knows each incident's true cause, so it can decide whether
a remediation actually recovers the service. The agent only ever sees signals.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

_HIDDEN = {"truth"}


@cache
def _incidents() -> dict[str, dict]:
    raw = resources.files("incident_copilot.data").joinpath("incidents.json").read_text()
    return {i["id"]: i for i in json.loads(raw)}


def incident_ids() -> list[str]:
    return sorted(_incidents())


def load(incident_id: str) -> dict:
    """What the agent may see: everything except the ground truth."""
    return {k: v for k, v in _incidents()[incident_id].items() if k not in _HIDDEN}


def truth(incident_id: str) -> dict:
    return _incidents()[incident_id]["truth"]


def apply(incident: dict, action: str) -> dict:
    expected = truth(incident["id"])["remediation"]
    recovered = action == expected
    return {
        "action": action,
        "recovered": recovered,
        "post_alert_state": "cleared" if recovered else "still firing",
    }
