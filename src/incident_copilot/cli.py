"""incident-copilot CLI: run an incident, approve or reject later, inspect history."""

from __future__ import annotations

import argparse
import json

from incident_copilot import runtime, simulator


def _print_update(update: dict) -> None:
    for node, value in update.items():
        if node == "__interrupt__":
            continue
        for line in (value or {}).get("audit", []):
            print(f"  [{node}] {line}")


def _report(st: runtime.RunStatus) -> None:
    if st.paused and st.question:
        print(f"\nPAUSED for approval (thread {st.thread_id}):")
        print(json.dumps(st.question, indent=2))
        print(f"\nResume with: incident-copilot resume {st.thread_id} --approve --approver YOU")
    else:
        print(f"\nFinished: status={st.state.get('status')} (thread {st.thread_id})\n")
        print(st.state.get("postmortem", ""))


def main() -> None:
    p = argparse.ArgumentParser(prog="incident-copilot")
    p.add_argument("--db", default="runs.sqlite", help="SQLite checkpoint database")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("incident_id")
    r.add_argument("--thread")
    s = sub.add_parser("resume")
    s.add_argument("thread")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    s.add_argument("--approver", default="cli-user")
    s.add_argument("--note", default="")
    s.add_argument("--from-checkpoint", help="fork from an earlier checkpoint (time travel)")
    h = sub.add_parser("history")
    h.add_argument("thread")
    args = p.parse_args()

    if args.cmd == "list":
        for iid in simulator.incident_ids():
            inc = simulator.load(iid)
            print(f"{iid}  {inc['alert']['name']:<15} {inc['service']:<16} {inc['title']}")
        return
    graph, conn = runtime.open_graph(args.db)
    try:
        if args.cmd == "run":
            _report(runtime.start(graph, args.incident_id, args.thread, _print_update))
        elif args.cmd == "resume":
            decision = {"approved": args.approve, "approver": args.approver, "note": args.note}
            if args.from_checkpoint:
                st = runtime.fork(graph, args.thread, args.from_checkpoint, decision, _print_update)
            else:
                st = runtime.resume(graph, args.thread, decision, _print_update)
            _report(st)
        elif args.cmd == "history":
            for row in runtime.history(graph, args.thread):
                print(f"step {row['step']:>3}  next={row['next']}  status={row['status']}  "
                      f"checkpoint={row['checkpoint_id']}")  # fmt: skip
    finally:
        conn.close()


if __name__ == "__main__":
    main()
