import asyncio

import pytest

from agora.matrix.sync import EventDispatcher
from agora.observe.sync_service import SyncService


async def test_sync_service_dispatches_queued_events(fake_matrix_client) -> None:
    dispatcher = EventDispatcher()
    received: list[tuple[str, dict]] = []

    async def _raw(room_id, event):
        received.append((room_id, event))

    dispatcher.on_raw_event(_raw)

    room_id = await fake_matrix_client.create_room("obs")
    fake_matrix_client.queue_event(room_id, {"type": "m.agora.task", "content": {}})

    service = SyncService(fake_matrix_client, dispatcher, rooms=[room_id], timeout_ms=10)
    await service.start()
    # Give the loop a few ticks to drain the queue.
    await asyncio.sleep(0.05)
    await service.stop()

    assert not service.running
    assert any(r == room_id for r, _ in received)


async def test_sync_service_filters_by_watched_rooms(fake_matrix_client) -> None:
    dispatcher = EventDispatcher()
    received: list[tuple[str, dict]] = []

    async def _raw(room_id, event):
        received.append((room_id, event))

    dispatcher.on_raw_event(_raw)

    r1 = await fake_matrix_client.create_room("watched")
    r2 = await fake_matrix_client.create_room("ignored")
    fake_matrix_client.queue_event(r1, {"type": "m.agora.learning", "content": {}})
    fake_matrix_client.queue_event(r2, {"type": "m.agora.learning", "content": {}})

    service = SyncService(fake_matrix_client, dispatcher, rooms=[r1], timeout_ms=10)
    await service.start()
    await asyncio.sleep(0.05)
    await service.stop()

    rooms_seen = {r for r, _ in received}
    assert r1 in rooms_seen
    assert r2 not in rooms_seen


async def test_sync_service_stop_is_idempotent(fake_matrix_client) -> None:
    dispatcher = EventDispatcher()
    service = SyncService(fake_matrix_client, dispatcher, rooms=None, timeout_ms=10)
    await service.start()
    await service.stop()
    await service.stop()  # second call must not raise


async def test_sync_service_survives_dispatcher_crashes(
    fake_matrix_client, monkeypatch, caplog
) -> None:
    dispatcher = EventDispatcher()

    async def _boom(_room, _event):
        raise RuntimeError("boom")

    dispatcher.on_raw_event(_boom)

    room_id = await fake_matrix_client.create_room("x")
    fake_matrix_client.queue_event(room_id, {"type": "m.agora.task", "content": {}})

    service = SyncService(fake_matrix_client, dispatcher, rooms=[room_id], timeout_ms=10)
    await service.start()
    await asyncio.sleep(0.05)
    await service.stop()
    # Loop survived — service is stopped cleanly (not crashed mid-run).
    assert service.running is False
