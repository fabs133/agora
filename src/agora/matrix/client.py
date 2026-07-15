"""Thin Matrix client wrapper around ``matrix-nio``.

Exposes a small, domain-shaped :class:`MatrixClientProtocol` so tests can inject a fake
without touching the network. :class:`AgoraMatrixClient` is the production implementation
that delegates to ``nio.AsyncClient``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nio import (
    AsyncClient,
    LoginError,
    LoginResponse,
    RoomCreateError,
    RoomCreateResponse,
    RoomGetStateError,
    RoomGetStateResponse,
    RoomMessagesError,
    RoomMessagesResponse,
    RoomPutStateError,
    RoomPutStateResponse,
    RoomSendError,
    RoomSendResponse,
    UploadError,
    UploadResponse,
)

from agora.core.errors import AgoraError
from agora.core.types import EventId, RoomId


@runtime_checkable
class MatrixClientProtocol(Protocol):
    """Minimal surface used by the rest of the codebase."""

    async def login(self, password: str) -> None: ...

    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        initial_state: list[dict[str, Any]] | None = None,
    ) -> RoomId: ...

    async def send_state_event(
        self,
        room_id: RoomId,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> EventId: ...

    async def send_event(
        self,
        room_id: RoomId,
        event_type: str,
        content: dict[str, Any],
    ) -> EventId: ...

    async def get_room_state(self, room_id: RoomId) -> list[dict[str, Any]]: ...

    async def get_room_timeline(
        self,
        room_id: RoomId,
        limit: int = 100,
        since: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def upload_file(self, file_path: str) -> str: ...

    async def download_file(self, mxc_uri: str, dest_dir: str) -> str: ...

    async def sync_once(
        self,
        timeout_ms: int = 30000,
        since: str | None = None,
        rooms: list[RoomId] | None = None,
    ) -> SyncBatch: ...

    async def close(self) -> None: ...


class NullMatrixClient:
    """A :class:`MatrixClientProtocol` that needs no homeserver.

    Used when the Matrix surface is switched off (``enable_observer=False``):
    the phased runner drives a whole lifecycle with nobody watching, and
    demanding a live Conduit for rooms no human will ever open is a hard
    dependency for nothing. With this client the documented path is Python +
    Ollama; Conduit becomes the optional live-observation view.

    **This is not a void.** Provenance is unconditional (F3): every call is
    recorded to ``run.log`` at INFO, so the record of what an agent tried to
    communicate survives whether or not a homeserver existed. What disappears
    is only the *delivery* to a room.

    Two things this deliberately does NOT do:

    * It does not fake a human. Tools that block on a person
      (``request_review``, ``await_user_decision``) must fail LOUDLY rather
      than receive a synthetic answer — a fabricated approval is worse than no
      approval. Those tools check ``ToolContext.matrix_live`` and refuse; the
      client never invents a vote.
    * It does not silently swallow. Room ids and event ids are synthetic but
      *marked* (``!null-…``/``$null-…``), so anything that logs or asserts on
      them shows plainly that no room existed.
    """

    def __init__(self, *, homeserver: str = "<null>", user_id: str = "@null:local") -> None:
        self.homeserver = homeserver
        self.user_id = user_id
        self._seq = 0
        self._log = logging.getLogger("agora.matrix.null")

    def _next(self, kind: str) -> str:
        self._seq += 1
        return f"{kind}null-{self._seq}"

    async def login(self, password: str) -> None:  # noqa: ARG002
        self._log.info("matrix: observer off — no login (NullMatrixClient)")

    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        initial_state: list[dict[str, Any]] | None = None,
    ) -> RoomId:
        room = RoomId(self._next("!"))
        self._log.info("matrix(null): create_room name=%r -> %s", name, room)
        return room

    async def send_state_event(
        self,
        room_id: RoomId,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> EventId:
        self._log.info("matrix(null): state %s room=%s key=%r", event_type, room_id, state_key)
        return EventId(self._next("$"))

    async def send_event(
        self,
        room_id: RoomId,
        event_type: str,
        content: dict[str, Any],
    ) -> EventId:
        # The content is the point: this line IS the delivery record.
        body = content.get("body") or content.get("message") or content.get("summary") or ""
        self._log.info(
            "matrix(null): event %s room=%s%s",
            event_type,
            room_id,
            f" :: {str(body)[:200]}" if body else "",
        )
        return EventId(self._next("$"))

    async def get_room_state(self, room_id: RoomId) -> list[dict[str, Any]]:  # noqa: ARG002
        return []

    async def get_room_timeline(
        self,
        room_id: RoomId,  # noqa: ARG002
        limit: int = 100,  # noqa: ARG002
        since: str | None = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        return []

    async def upload_file(self, file_path: str) -> str:
        self._log.info("matrix(null): upload_file %s (not uploaded)", file_path)
        return f"mxc://null/{Path(file_path).name}"

    async def download_file(self, mxc_uri: str, dest_dir: str) -> str:
        raise AgoraError(
            f"cannot download {mxc_uri}: the Matrix surface is off "
            "(enable_observer=False). Nothing was ever uploaded to fetch."
        )

    async def sync_once(
        self,
        timeout_ms: int = 30000,  # noqa: ARG002
        since: str | None = None,
        rooms: list[RoomId] | None = None,  # noqa: ARG002
    ) -> SyncBatch:
        # No events will ever arrive; returning empty forever is honest, and the
        # sync service is not started when the observer is off anyway.
        return SyncBatch(events=[], next_since=since)

    async def close(self) -> None:
        return None


@dataclass
class SyncBatch:
    """One batch of events returned by ``sync_once``.

    ``events`` is a list of ``(room_id, event_dict)`` tuples, each shaped like a
    raw Matrix timeline event (``type``, ``event_id``, ``sender``, ``content``,
    optional ``state_key``). ``next_since`` is the token to pass to the next
    ``sync_once`` call.
    """

    events: list[tuple[RoomId, dict[str, Any]]]
    next_since: str | None


class AgoraMatrixClient:
    """Concrete :class:`MatrixClientProtocol` backed by ``nio.AsyncClient``."""

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        device_name: str = "agora",
        client: AsyncClient | None = None,
    ) -> None:
        self.homeserver = homeserver
        self.user_id = user_id
        self.device_name = device_name
        self._client = client or AsyncClient(homeserver, user_id)

    async def login(self, password: str) -> None:
        resp = await self._client.login(password=password, device_name=self.device_name)
        if isinstance(resp, LoginError) or not isinstance(resp, LoginResponse):
            raise AgoraError(f"Matrix login failed: {getattr(resp, 'message', resp)}")

    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        initial_state: list[dict[str, Any]] | None = None,
    ) -> RoomId:
        resp = await self._client.room_create(
            name=name,
            topic=topic,
            invite=invite or [],
            initial_state=initial_state or [],
        )
        if isinstance(resp, RoomCreateError) or not isinstance(resp, RoomCreateResponse):
            raise AgoraError(f"room_create failed: {getattr(resp, 'message', resp)}")
        return resp.room_id

    async def send_state_event(
        self,
        room_id: RoomId,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> EventId:
        resp = await self._client.room_put_state(
            room_id=room_id,
            event_type=event_type,
            content=content,
            state_key=state_key,
        )
        if isinstance(resp, RoomPutStateError) or not isinstance(resp, RoomPutStateResponse):
            raise AgoraError(f"room_put_state failed: {getattr(resp, 'message', resp)}")
        return resp.event_id

    async def send_event(
        self,
        room_id: RoomId,
        event_type: str,
        content: dict[str, Any],
    ) -> EventId:
        """Post a timeline event.

        Some homeservers (notably Conduit) reject custom event types in
        ``/send`` with HTTP 500. We wrap any non-``m.room.*`` type into an
        ``m.room.message`` envelope with ``com.agora.type`` / ``com.agora.data``
        for loss-free round-trip. :func:`unwrap_envelope` on the read side
        restores the original type.
        """
        wire_type, wire_content = wrap_for_homeserver(event_type, content)
        resp = await self._client.room_send(
            room_id=room_id,
            message_type=wire_type,
            content=wire_content,
        )
        if isinstance(resp, RoomSendError) or not isinstance(resp, RoomSendResponse):
            raise AgoraError(
                f"room_send failed for {event_type} (wire={wire_type}) in {room_id}: "
                f"{_describe_matrix_response(resp)}"
            )
        return resp.event_id

    async def get_room_state(self, room_id: RoomId) -> list[dict[str, Any]]:
        resp = await self._client.room_get_state(room_id=room_id)
        if isinstance(resp, RoomGetStateError) or not isinstance(resp, RoomGetStateResponse):
            raise AgoraError(f"room_get_state failed: {getattr(resp, 'message', resp)}")
        return list(resp.events)

    async def get_room_timeline(
        self,
        room_id: RoomId,
        limit: int = 100,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        resp = await self._client.room_messages(
            room_id=room_id,
            start=since or "",
            limit=limit,
        )
        if isinstance(resp, RoomMessagesError) or not isinstance(resp, RoomMessagesResponse):
            raise AgoraError(f"room_messages failed: {getattr(resp, 'message', resp)}")
        return [unwrap_envelope(self._event_to_dict(ev)) for ev in resp.chunk]

    async def upload_file(self, file_path: str) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise AgoraError(f"upload_file: not a regular file: {file_path}")
        content_type = _guess_content_type(path)
        async with path.open("rb") as fh:  # type: ignore[attr-defined]
            resp, _ = await self._client.upload(
                fh,
                content_type=content_type,
                filename=path.name,
                filesize=path.stat().st_size,
            )
        if isinstance(resp, UploadError) or not isinstance(resp, UploadResponse):
            raise AgoraError(f"upload failed: {getattr(resp, 'message', resp)}")
        return resp.content_uri

    async def download_file(self, mxc_uri: str, dest_dir: str) -> str:
        """Download an ``mxc://`` URI into ``dest_dir`` and return the local path.

        Caches by a hash of the MXC URI; subsequent calls with the same URI
        return the cached path without re-downloading.
        """
        import hashlib

        if not mxc_uri.startswith("mxc://"):
            raise AgoraError(f"download_file: not an mxc URI: {mxc_uri!r}")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256(mxc_uri.encode("utf-8")).hexdigest()[:16]
        # The original filename isn't recoverable from the URI alone; use a prefix.
        target = dest / f"kb-{digest}"
        if target.exists():
            return str(target)

        # Strip the mxc:// prefix; nio.AsyncClient.download takes (server_name, media_id).
        remainder = mxc_uri[len("mxc://") :]
        try:
            server_name, media_id = remainder.split("/", 1)
        except ValueError as exc:
            raise AgoraError(f"malformed mxc uri: {mxc_uri!r}") from exc

        resp = await self._client.download(server_name=server_name, media_id=media_id)
        body = getattr(resp, "body", None)
        if not isinstance(body, (bytes, bytearray)):
            raise AgoraError(f"download failed for {mxc_uri}: {getattr(resp, 'message', resp)}")

        # Preserve suggested filename if provided.
        suggested = getattr(resp, "filename", None)
        if isinstance(suggested, str) and suggested:
            target = dest / f"kb-{digest}-{Path(suggested).name}"
        target.write_bytes(bytes(body))
        return str(target)

    async def sync_once(
        self,
        timeout_ms: int = 30000,
        since: str | None = None,
        rooms: list[RoomId] | None = None,
    ) -> SyncBatch:
        """Long-poll ``/sync`` once. Returns one batch of events + the next token."""
        kwargs: dict[str, Any] = {"timeout": timeout_ms}
        if since:
            kwargs["since"] = since
        resp = await self._client.sync(**kwargs)
        # nio's SyncResponse has .next_batch and .rooms.join[...].timeline.events
        next_since = getattr(resp, "next_batch", None)
        events: list[tuple[RoomId, dict[str, Any]]] = []
        rooms_obj = getattr(resp, "rooms", None)
        join_rooms = getattr(rooms_obj, "join", {}) if rooms_obj else {}
        watched = set(rooms) if rooms else None
        for room_id, room in join_rooms.items():
            if watched is not None and room_id not in watched:
                continue
            timeline = getattr(room, "timeline", None)
            raw_events = getattr(timeline, "events", []) if timeline else []
            for ev in raw_events:
                events.append((room_id, unwrap_envelope(self._event_to_dict(ev))))
        return SyncBatch(events=events, next_since=next_since)

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _event_to_dict(event: Any) -> dict[str, Any]:
        source = getattr(event, "source", None)
        if isinstance(source, dict):
            return source
        return {
            "type": getattr(event, "type", ""),
            "event_id": getattr(event, "event_id", ""),
            "sender": getattr(event, "sender", ""),
            "content": getattr(event, "content", {}) or {},
        }


AGORA_ENVELOPE_TYPE_KEY = "com.agora.type"
AGORA_ENVELOPE_DATA_KEY = "com.agora.data"


def wrap_for_homeserver(event_type: str, content: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a domain event type + content to what we actually send over the wire.

    Only Agora's **own** custom events (``m.agora.*``) are wrapped in an
    ``m.room.message`` envelope — those are the ones Conduit rejects with HTTP
    500. Every other event type (``m.room.*``, ``m.poll.*``, ``m.reaction``,
    ``m.space.*``, ...) is Matrix-spec and passes through so native clients
    like Element render them correctly.
    """
    if not event_type.startswith("m.agora."):
        return event_type, content
    fallback_body = _envelope_body_fallback(event_type, content)
    return "m.room.message", {
        "msgtype": "m.notice",
        "body": fallback_body,
        AGORA_ENVELOPE_TYPE_KEY: event_type,
        AGORA_ENVELOPE_DATA_KEY: content,
    }


def unwrap_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """If ``event`` is an Agora envelope, return an event dict with the original
    ``type``/``content``. Otherwise return ``event`` unchanged.
    """
    if not isinstance(event, dict):
        return event
    if event.get("type") != "m.room.message":
        return event
    content = event.get("content") or {}
    inner_type = content.get(AGORA_ENVELOPE_TYPE_KEY)
    inner_data = content.get(AGORA_ENVELOPE_DATA_KEY)
    if not isinstance(inner_type, str) or not isinstance(inner_data, dict):
        return event
    return {**event, "type": inner_type, "content": inner_data}


def _envelope_body_fallback(event_type: str, content: dict[str, Any]) -> str:
    """Best-effort plain body so raw Matrix clients still show something useful."""
    snippets: list[str] = [f"[{event_type}]"]
    for key in ("task_id", "to_phase", "category", "content", "description", "fingerprint"):
        if key in content and content[key] not in (None, ""):
            value = str(content[key])
            snippets.append(f"{key}={value[:80]}")
    return " ".join(snippets)


def _guess_content_type(path: Path) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _describe_matrix_response(resp: Any) -> str:
    """Extract as much detail as possible from a matrix-nio response for error messages."""
    parts: list[str] = [type(resp).__name__]
    for attr in ("status_code", "errcode", "message", "retry_after_ms"):
        value = getattr(resp, attr, None)
        if value not in (None, ""):
            parts.append(f"{attr}={value!r}")
    transport = getattr(resp, "transport_response", None)
    if transport is not None:
        status = getattr(transport, "status", None)
        if status is not None:
            parts.append(f"http_status={status}")
    return " ".join(parts)
