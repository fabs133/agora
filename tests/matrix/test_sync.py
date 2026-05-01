from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import LearningCategory, ProjectPhase, TaskStatus
from agora.matrix.events import (
    LEARNING_EVENT,
    PHASE_CHANGE_EVENT,
    TASK_EVENT,
    TASK_RESULT_EVENT,
    learning_to_content,
    phase_change_to_content,
    task_result_to_content,
    task_to_content,
)
from agora.matrix.sync import EventDispatcher

ROOM = "!room:agora.local"


async def test_dispatcher_routes_task_events() -> None:
    dispatcher = EventDispatcher()
    received: list[tuple[str, dict]] = []

    async def handler(room_id, parsed):
        received.append((room_id, parsed))

    dispatcher.on_task_event(handler)
    await dispatcher.handle(
        ROOM,
        {
            "type": TASK_EVENT,
            "content": task_to_content(
                task_id="t1",
                description="x",
                agent_id=None,
                status=TaskStatus.PENDING,
                fingerprint="fp",
            ),
        },
    )
    assert len(received) == 1
    assert received[0][0] == ROOM
    assert received[0][1]["task_id"] == "t1"


async def test_dispatcher_routes_task_result_events() -> None:
    dispatcher = EventDispatcher()
    received = []

    async def handler(room_id, parsed):
        received.append(parsed)

    dispatcher.on_task_result(handler)
    await dispatcher.handle(
        ROOM,
        {
            "type": TASK_RESULT_EVENT,
            "content": task_result_to_content(
                task_id="t1", success=True, output="", artifacts=[], postcondition_results=[]
            ),
        },
    )
    assert received and received[0]["task_id"] == "t1"


async def test_dispatcher_routes_learning_events() -> None:
    dispatcher = EventDispatcher()
    received: list[Learning] = []

    async def handler(room_id, learning):
        received.append(learning)

    dispatcher.on_learning(handler)
    learning = Learning(
        id="l1",
        category=LearningCategory.PATTERN,
        content="x",
        confidence=0.7,
        task_ref="t1",
    )
    await dispatcher.handle(
        ROOM,
        {"type": LEARNING_EVENT, "content": learning_to_content(learning)},
    )
    assert received == [learning]


async def test_dispatcher_routes_phase_change_events() -> None:
    dispatcher = EventDispatcher()
    received: list[PhaseChange] = []

    async def handler(room_id, change):
        received.append(change)

    dispatcher.on_phase_change(handler)
    change = PhaseChange(
        from_phase=ProjectPhase.REVIEW,
        to_phase=ProjectPhase.IMPLEMENTATION,
        reason="fix",
        timestamp="2026-04-15T00:00:00+00:00",
    )
    await dispatcher.handle(
        ROOM,
        {"type": PHASE_CHANGE_EVENT, "content": phase_change_to_content(change)},
    )
    assert received == [change]


async def test_dispatcher_ignores_unknown_events() -> None:
    dispatcher = EventDispatcher()
    called = False

    async def handler(room_id, payload):
        nonlocal called
        called = True

    dispatcher.on_task_event(handler)
    await dispatcher.handle(ROOM, {"type": "m.room.message", "content": {"body": "hi"}})
    await dispatcher.handle(ROOM, {})  # no type at all
    assert called is False


async def test_dispatcher_drops_malformed_events() -> None:
    dispatcher = EventDispatcher()
    called = False

    async def handler(room_id, payload):
        nonlocal called
        called = True

    dispatcher.on_task_event(handler)
    # Missing required fields — parser raises, dispatcher swallows.
    await dispatcher.handle(ROOM, {"type": TASK_EVENT, "content": {"not": "valid"}})
    assert called is False


async def test_dispatcher_fanout_to_multiple_handlers() -> None:
    dispatcher = EventDispatcher()
    calls = []

    async def h1(room_id, payload):
        calls.append("h1")

    async def h2(room_id, payload):
        calls.append("h2")

    dispatcher.on_task_event(h1)
    dispatcher.on_task_event(h2)
    await dispatcher.handle(
        ROOM,
        {
            "type": TASK_EVENT,
            "content": task_to_content(
                task_id="t1",
                description="x",
                agent_id=None,
                status=TaskStatus.PENDING,
                fingerprint="fp",
            ),
        },
    )
    assert calls == ["h1", "h2"]
