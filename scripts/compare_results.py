"""Compare checked-in eval results with a fresh run (timestamps excluded)."""

import json
import sys


def load(path: str) -> dict:
    report = json.loads(open(path).read())
    report.pop("generated_at", None)
    report["time_travel"].pop("forked_from", None)  # checkpoint ids are random per run
    return report


if load(sys.argv[1]) != load(sys.argv[2]):
    print("Checked-in results are stale; run `python -m incident_copilot.evals`.")
    sys.exit(1)
print("Checked-in eval results match a fresh run.")
