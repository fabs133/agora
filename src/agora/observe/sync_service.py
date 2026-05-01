"""Matrix sync-loop driver.

Wraps :meth:`MatrixClientProtocol.sync_once` in an infinite loop, feeding each
batch into an :class:`EventDispatcher`. Cancellation-safe via :meth:`stop`.

Usage::

    service = SyncService(client, dispatcher, rooms=[project_room])
    await service.start()          # fire-and-forget background task
    ...
    await service.stop()           # cancels cleanly

Or drive it synchronously in foreground::

    await service.run()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agora.core.types import RoomId
from agora.matrix.client import MatrixClientProtocol
from agora.matrix.sync import EventDispatcher

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        client: MatrixClientProtocol,
        dispatcher: EventDispatcher,
        rooms: list[RoomId] | None = None,
        timeout_ms: int = 30_000,
        poll_interval_on_error: float = 2.0,
    ) -> None:
        self._client = client
        self._dispatcher = dispatcher
        self._rooms = list(rooms) if rooms else None
        self._timeout_ms = timeout_ms
        self._error_backoff = poll_interval_on_error
        self._since: str | None = None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def since(self) -> str | None:
        return self._since

    def watch_rooms(self, rooms: list[RoomId]) -> None:
        """Replace the watched-room set mid-run. Events in other rooms are dropped."""
        self._rooms = list(rooms)

    async def start(self) -> None:
        """Launch the sync loop as a background task. Idempotent."""
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="agora-sync")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._task = None

    async def run(self) -> None:
        """Drive the sync loop until :meth:`stop` is called."""
        logger.info("sync_service: starting loop (rooms=%s)", self._rooms)
        idle_backoff = max(0.01, self._timeout_ms / 1000)
        while not self._stop_event.is_set():
            try:
                batch = await self._client.sync_once(
                    timeout_ms=self._timeout_ms,
                    since=self._since,
                    rooms=self._rooms,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("sync_once failed: %s; backing off %.1fs", exc, self._error_backoff)
                await _sleep_or_stop(self._stop_event, self._error_backoff)
                continue

            for room_id, event in batch.events:
                if self._stop_event.is_set():
                    break
                try:
                    await self._dispatcher.handle(room_id, event)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "dispatcher raised on %s in %s: %s",
                        event.get("type"),
                        room_id,
                        exc,
                    )

            self._since = batch.next_since
            # Yield the loop — if sync_once returned instantly (fake client or
            # real server that short-polled), sleep briefly so stop() can fire
            # and so we don't spin on an idle homeserver.
            if not batch.events:
                await _sleep_or_stop(self._stop_event, idle_backoff)
            else:
                await asyncio.sleep(0)
        logger.info("sync_service: loop exited (since=%s)", self._since)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds`` but wake immediately when ``stop`` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


__all__ = ["SyncService"]


# Re-export for convenience in tests that import from this module.
def _drain_attributes(_: Any) -> None:  # pragma: no cover — documentation anchor
    """Placeholder to keep pyflakes happy about :mod:`typing.Any`."""
    return None
