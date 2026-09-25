"""Run, pause, resume, and inspect incident threads against a durable SQLite checkpointer."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from incident_copilot.graph import build_graph


def open_graph(db_path: str = "runs.sqlite"):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return build_graph(SqliteSaver(conn)), conn


@dataclass
class RunStatus:
    thread_id: str
    paused: bool
    question: dict | None
    state: dict


def _config(thread_id: str, checkpoint_id: str | None = None) -> dict:
    conf = {"thread_id": thread_id}
    if checkpoint_id:
        conf |= {"checkpoint_id": checkpoint_id, "checkpoint_ns": ""}
    return {"configurable": conf}


def status(graph, thread_id: str) -> RunStatus:
    snap = graph.get_state(_config(thread_id))
    question = snap.interrupts[0].value if snap.interrupts else None
    return RunStatus(thread_id, bool(snap.next), question, dict(snap.values))


def start(graph, incident_id: str, thread_id: str | None = None, on_update=None) -> RunStatus:
    thread_id = thread_id or f"{incident_id}-{uuid.uuid4().hex[:8]}"
    for update in graph.stream(
        {"incident_id": incident_id}, _config(thread_id), stream_mode="updates"
    ):
        if on_update:
            on_update(update)
    return status(graph, thread_id)


def _drain(stream, on_update) -> None:
    for update in stream:
        if on_update:
            on_update(update)


def resume(graph, thread_id: str, decision: dict, on_update=None) -> RunStatus:
    """Answer the pending approval question of a paused thread."""
    _drain(graph.stream(Command(resume=decision), _config(thread_id), stream_mode="updates"),
           on_update)  # fmt: skip
    return status(graph, thread_id)


def fork(graph, thread_id: str, checkpoint_id: str, decision: dict, on_update=None) -> RunStatus:
    """Time travel: branch from an earlier checkpoint and answer its approval differently.

    Resuming an old checkpoint directly would replay the answer already stored there, so we
    write a new checkpoint on top of it (a fork), run up to the approval interrupt again,
    and resume that branch with the new decision. The original branch stays in history.
    """
    fork_config = graph.update_state(
        _config(thread_id, checkpoint_id),
        {"audit": [f"fork: replaying from checkpoint {checkpoint_id[:8]}"]},
        as_node="propose",
    )
    _drain(graph.stream(None, fork_config, stream_mode="updates"), on_update)
    return resume(graph, thread_id, decision, on_update)


def history(graph, thread_id: str) -> list[dict]:
    rows = []
    for snap in graph.get_state_history(_config(thread_id)):
        rows.append({
            "checkpoint_id": snap.config["configurable"]["checkpoint_id"],
            "step": snap.metadata.get("step"),
            "next": list(snap.next),
            "status": snap.values.get("status"),
        })  # fmt: skip
    return rows
