"""Renders domain events into Matrix messages and posts them to project rooms.

Subscribes to :class:`~agora.matrix.sync.EventDispatcher` and, for each event
type we care about, posts a human-readable ``m.room.message`` back to the same
room. Deduplicates its own output by checking sender to avoid echo loops.
"""

from __future__ import annotations

import logging
from typing import Any

from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import RoomId
from agora.matrix.client import MatrixClientProtocol
from agora.matrix.sync import EventDispatcher
from agora.observe import formatters

logger = logging.getLogger(__name__)


class Renderer:
    """Subscribes to the event dispatcher and posts a formatted message back to
    the project room for each domain event, delegating the HTML to
    :mod:`agora.observe.formatters`. Echo-safe: it ignores events from its own
    sender so its posts don't re-trigger it."""

    def __init__(
        self,
        matrix_client: MatrixClientProtocol,
        dispatcher: EventDispatcher,
        self_user_id: str = "",
    ) -> None:
        self._client = matrix_client
        self._dispatcher = dispatcher
        self._self_user_id = self_user_id
        self._attached = False

    def attach(self) -> None:
        """Register handlers. Idempotent."""
        if self._attached:
            return
        self._dispatcher.on_phase_change(self._on_phase_change)
        self._dispatcher.on_task_event(self._on_task_event)
        self._dispatcher.on_task_result(self._on_task_result)
        self._dispatcher.on_learning(self._on_learning)
        self._attached = True

    # --- handlers ----------------------------------------------------------

    async def _on_phase_change(self, room_id: RoomId, change: PhaseChange) -> None:
        await self._post(room_id, formatters.format_phase_change(change))

    async def _on_task_event(self, room_id: RoomId, parsed: dict[str, Any]) -> None:
        await self._post(room_id, formatters.format_task_started(parsed))

    async def _on_task_result(self, room_id: RoomId, parsed: dict[str, Any]) -> None:
        await self._post(room_id, formatters.format_task_completed(parsed))

    async def _on_learning(self, room_id: RoomId, learning: Learning) -> None:
        await self._post(room_id, formatters.format_learning(learning))

    async def _post(self, room_id: RoomId, message: formatters.FormattedMessage) -> None:
        try:
            await self._client.send_event(room_id, "m.room.message", message.to_content())
        except Exception as exc:  # noqa: BLE001
            logger.warning("renderer failed to post to %s: %s", room_id, exc)


__all__ = ["Renderer"]
