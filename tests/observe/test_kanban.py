from agora.core.types import TaskStatus
from agora.observe.kanban import build_kanban


def _task_event(
    tid: str,
    status: str = "pending",
    description: str = "",
    agent: str | None = None,
) -> dict:
    return {
        "type": "m.agora.task",
        "content": {
            "task_id": tid,
            "description": description,
            "agent_id": agent,
            "status": status,
            "fingerprint": "",
            "timestamp": "2026-04-15T00:00:00+00:00",
        },
    }


def _result_event(tid: str, success: bool) -> dict:
    return {
        "type": "m.agora.task_result",
        "content": {
            "task_id": tid,
            "success": success,
            "output": "",
            "artifacts": [],
            "postcondition_results": [],
            "timestamp": "2026-04-15T00:10:00+00:00",
        },
    }


def test_empty_event_stream_gives_empty_columns() -> None:
    board = build_kanban([])
    assert all(len(cards) == 0 for cards in board.columns.values())


def test_task_events_populate_columns() -> None:
    events = [
        _task_event("t1", status="pending", description="A"),
        _task_event("t2", status="running", description="B"),
        _task_event("t3", status="done", description="C"),
    ]
    board = build_kanban(events)
    assert len(board.columns[TaskStatus.PENDING]) == 1
    assert len(board.columns[TaskStatus.RUNNING]) == 1
    assert len(board.columns[TaskStatus.DONE]) == 1


def test_result_event_flips_status_to_done() -> None:
    events = [
        _task_event("t1", status="running", description="A"),
        _result_event("t1", success=True),
    ]
    board = build_kanban(events)
    assert len(board.columns[TaskStatus.DONE]) == 1
    assert len(board.columns[TaskStatus.RUNNING]) == 0


def test_result_event_flips_status_to_failed() -> None:
    events = [
        _task_event("t1", status="running", description="A"),
        _result_event("t1", success=False),
    ]
    board = build_kanban(events)
    assert len(board.columns[TaskStatus.FAILED]) == 1


def test_latest_task_event_wins() -> None:
    events = [
        _task_event("t1", status="pending"),
        _task_event("t1", status="assigned"),
    ]
    board = build_kanban(events)
    assert len(board.columns[TaskStatus.PENDING]) == 0
    assert len(board.columns[TaskStatus.ASSIGNED]) == 1


def test_malformed_events_are_skipped() -> None:
    events = [
        {"type": "m.agora.task", "content": {}},  # missing required fields
        _task_event("ok", status="done"),
    ]
    board = build_kanban(events)
    # Only the valid event survives.
    assert len(board.columns[TaskStatus.DONE]) == 1


def test_to_dict_round_trip() -> None:
    events = [_task_event("t1", status="done", description="X")]
    dumped = build_kanban(events).to_dict()
    assert "done" in dumped
    assert dumped["done"][0]["id"] == "t1"
