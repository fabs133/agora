"""Event stream → domain-event dispatcher.

Components register async handlers for specific domain events. :meth:`EventDispatcher.handle`
takes a raw Matrix event dict, parses it, and fans out to all registered handlers.

The dispatcher intentionally *does not* own a sync loop — Sprint 2 keeps the surface small.
A production sync loop is wired up by the orchestrator (Sprint 3), which calls
``handle`` for each event yielded by ``AsyncClient.sync_forever``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agora.core.errors import AgoraError
from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import EventId, RoomId
from agora.matrix.events import (
    LEARNING_EVENT,
    PHASE_CHANGE_EVENT,
    TASK_EVENT,
    TASK_RESULT_EVENT,
    is_agora_event,
    learning_from_content,
    phase_change_from_content,
    task_from_content,
    task_result_from_content,
)


@dataclass(frozen=True)
class ReactionEvent:
    """Parsed ``m.reaction`` pointing at a target message."""

    event_id: EventId          # the reaction event's own id
    target_event_id: EventId   # the message being reacted to
    key: str                   # e.g. "✅", "🔁", "💬"
    sender: str                # user id of the reactor


@dataclass(frozen=True)
class ReplyEvent:
    """A ``m.room.message`` that carries ``m.in_reply_to`` to another event."""

    event_id: EventId
    target_event_id: EventId
    body: str
    sender: str

logger = logging.getLogger(__name__)

TaskHandler = Callable[[RoomId, dict[str, Any]], Awaitable[None]]
TaskResultHandler = Callable[[RoomId, dict[str, Any]], Awaitable[None]]
PhaseChangeHandler = Callable[[RoomId, PhaseChange], Awaitable[None]]
LearningHandler = Callable[[RoomId, Learning], Awaitable[None]]
PollResponseHandler = Callable[[RoomId, "PollResponse"], Awaitable[None]]
CommandHandler = Callable[[RoomId, "ParsedCommand"], Awaitable[None]]
ReactionHandler = Callable[[RoomId, ReactionEvent], Awaitable[None]]
ReplyHandler = Callable[[RoomId, ReplyEvent], Awaitable[None]]
RawEventHandler = Callable[[RoomId, dict[str, Any]], Awaitable[None]]

# Forward declarations: observe/polls.py and observe/commands.py define these.
# Using TYPE_CHECKING would break the runtime registration path, so we accept
# any object that carries the expected attributes.
PollResponse = Any  # observe.polls.PollResponse
ParsedCommand = Any  # observe.commands.ParsedCommand


# Matrix poll event types (MSC3381 — stable + unstable namespaces).
_POLL_RESPONSE_TYPES = frozenset(
    {"m.poll.response", "org.matrix.msc3381.poll.response"}
)
_MESSAGE_EVENT_TYPE = "m.room.message"
_REACTION_EVENT_TYPE = "m.reaction"


class EventDispatcher:
    """Routes parsed Agora events (and a few Matrix-native ones we care about).

    Components register async handlers for specific event types via the
    ``on_*`` methods; :meth:`handle` takes a raw Matrix event dict, parses
    it into the matching domain type (``Task``, ``TaskResult``,
    :class:`~agora.core.project.PhaseChange`, :class:`~agora.core.learning.Learning`,
    :class:`ReactionEvent`, :class:`ReplyEvent`, plus MSC3381 poll responses
    and ``/agora`` commands), and fans out to every registered handler in
    registration order.

    Idempotency: every parsed event is keyed by its Matrix ``event_id`` and
    duplicate deliveries (which Conduit can produce on reconnect) are
    silently skipped. Handlers run sequentially within a single event;
    parallelism between distinct events is the caller's responsibility.

    The dispatcher does not own a sync loop — the orchestrator wires one
    up via :class:`agora.observe.sync_service.SyncService`, which calls
    :meth:`handle` for each event yielded by ``AsyncClient.sync_forever``.
    """

    def __init__(self) -> None:
        self._task_handlers: list[TaskHandler] = []
        self._task_result_handlers: list[TaskResultHandler] = []
        self._phase_change_handlers: list[PhaseChangeHandler] = []
        self._learning_handlers: list[LearningHandler] = []
        self._poll_response_handlers: list[PollResponseHandler] = []
        self._command_handlers: list[CommandHandler] = []
        self._reaction_handlers: list[ReactionHandler] = []
        self._reply_handlers: list[ReplyHandler] = []
        self._raw_event_handlers: list[RawEventHandler] = []
        self._seen_event_ids: set[EventId] = set()

    # ---- registration ----
    def on_task_event(self, handler: TaskHandler) -> None:
        self._task_handlers.append(handler)

    def on_task_result(self, handler: TaskResultHandler) -> None:
        self._task_result_handlers.append(handler)

    def on_phase_change(self, handler: PhaseChangeHandler) -> None:
        self._phase_change_handlers.append(handler)

    def on_learning(self, handler: LearningHandler) -> None:
        self._learning_handlers.append(handler)

    def on_poll_response(self, handler: PollResponseHandler) -> None:
        self._poll_response_handlers.append(handler)

    def on_command(self, handler: CommandHandler) -> None:
        self._command_handlers.append(handler)

    def on_reaction(self, handler: ReactionHandler) -> None:
        """Fires when a user reacts to a message with an emoji."""
        self._reaction_handlers.append(handler)

    def on_reply(self, handler: ReplyHandler) -> None:
        """Fires when a user replies to a message (``m.in_reply_to`` relation)."""
        self._reply_handlers.append(handler)

    def on_raw_event(self, handler: RawEventHandler) -> None:
        """Fires for *every* event seen, after type-specific handlers."""
        self._raw_event_handlers.append(handler)

    # ---- dispatch ----
    async def handle(self, room_id: RoomId, event: dict[str, Any]) -> None:
        """Parse one Matrix event dict and dispatch to handlers. Unknown types are ignored."""
        event_id = event.get("event_id")
        if event_id and event_id in self._seen_event_ids:
            return
        if event_id:
            self._seen_event_ids.add(event_id)

        etype = event.get("type")
        content = event.get("content") or {}

        try:
            if etype and is_agora_event(etype):
                await self._dispatch_agora(etype, room_id, content)
            elif etype in _POLL_RESPONSE_TYPES:
                await self._dispatch_poll_response(room_id, event)
            elif etype == _REACTION_EVENT_TYPE:
                await self._dispatch_reaction(room_id, event)
            elif etype == _MESSAGE_EVENT_TYPE:
                await self._dispatch_message(room_id, event)
        except AgoraError as exc:
            logger.warning("dropping malformed %s event in %s: %s", etype, room_id, exc)

        for handler in self._raw_event_handlers:
            try:
                await handler(room_id, event)
            except Exception as exc:  # noqa: BLE001
                logger.exception("raw_event handler crashed on %s: %s", etype, exc)

    async def _dispatch_agora(
        self, etype: str, room_id: RoomId, content: dict[str, Any]
    ) -> None:
        if etype == TASK_EVENT:
            await self._fanout(self._task_handlers, room_id, task_from_content(content))
        elif etype == TASK_RESULT_EVENT:
            await self._fanout(
                self._task_result_handlers, room_id, task_result_from_content(content)
            )
        elif etype == PHASE_CHANGE_EVENT:
            await self._fanout(
                self._phase_change_handlers, room_id, phase_change_from_content(content)
            )
        elif etype == LEARNING_EVENT:
            await self._fanout(
                self._learning_handlers, room_id, learning_from_content(content)
            )

    async def _dispatch_poll_response(
        self, room_id: RoomId, event: dict[str, Any]
    ) -> None:
        if not self._poll_response_handlers:
            return
        # Lazy import to avoid circular (observe.polls imports agora.core types).
        from agora.observe.polls import parse_poll_response

        parsed = parse_poll_response(event)
        if parsed is None:
            return
        await self._fanout(self._poll_response_handlers, room_id, parsed)

    async def _dispatch_message(
        self, room_id: RoomId, event: dict[str, Any]
    ) -> None:
        content = event.get("content") or {}
        body = content.get("body")
        if not isinstance(body, str):
            return

        # Thread-aware: if this message replies to another, fan out to reply
        # handlers FIRST. Reply handlers decide whether the reply should also
        # be treated as a command (they typically don't, but we still give
        # slash commands a pass-through path below for /agora review etc.).
        target_event_id = _extract_reply_target(content)
        if target_event_id and self._reply_handlers:
            reply = ReplyEvent(
                event_id=str(event.get("event_id", "")),
                target_event_id=target_event_id,
                body=_strip_matrix_reply_fallback(body),
                sender=str(event.get("sender", "")),
            )
            await self._fanout(self._reply_handlers, room_id, reply)

        if not self._command_handlers:
            return
        from agora.observe.commands import parse_command

        parsed = parse_command(body, sender=event.get("sender", ""))
        if parsed is None:
            return
        await self._fanout(self._command_handlers, room_id, parsed)

    async def _dispatch_reaction(
        self, room_id: RoomId, event: dict[str, Any]
    ) -> None:
        if not self._reaction_handlers:
            return
        content = event.get("content") or {}
        relates = content.get("m.relates_to") or {}
        if not isinstance(relates, dict):
            return
        if relates.get("rel_type") != "m.annotation":
            return
        target = relates.get("event_id")
        key = relates.get("key")
        if not isinstance(target, str) or not isinstance(key, str):
            return
        reaction = ReactionEvent(
            event_id=str(event.get("event_id", "")),
            target_event_id=target,
            key=key,
            sender=str(event.get("sender", "")),
        )
        await self._fanout(self._reaction_handlers, room_id, reaction)

    @staticmethod
    async def _fanout(
        handlers: list[Callable[[RoomId, Any], Awaitable[None]]],
        room_id: RoomId,
        payload: Any,
    ) -> None:
        for handler in handlers:
            await handler(room_id, payload)


def _extract_reply_target(content: dict[str, Any]) -> EventId | None:
    """Return the ``event_id`` this message replies to, or ``None``.

    Matrix encodes replies under ``content.m.relates_to.m.in_reply_to.event_id``
    (same shape Element sends on tap-reply).
    """
    relates = content.get("m.relates_to")
    if not isinstance(relates, dict):
        return None
    in_reply = relates.get("m.in_reply_to")
    if not isinstance(in_reply, dict):
        return None
    event_id = in_reply.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    return None


def _strip_matrix_reply_fallback(body: str) -> str:
    """Matrix ``m.in_reply_to`` bodies are prefixed with a quoted fallback:

        > <@alice:example.org> original message text
        > continued
        (blank line)
        actual reply text

    The leading ``> `` lines are a client-rendered quote of what's being replied
    to and are noise for our routing. Strip them so handlers see only the
    user's actual reply text.
    """
    lines = body.splitlines()
    # Find the first line that is not a ``> `` quote AND is preceded by a blank line.
    cleaned: list[str] = []
    skipping = True
    for line in lines:
        if skipping and (line.startswith("> ") or line.startswith(">")):
            continue
        if skipping and line.strip() == "":
            # blank separator — end of quote block
            skipping = False
            continue
        skipping = False
        cleaned.append(line)
    stripped = "\n".join(cleaned).strip()
    # If stripping killed everything, fall back to the raw body so we never
    # route an empty reply.
    return stripped or body.strip()
