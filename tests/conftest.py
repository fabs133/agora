"""Shared fixtures. ``FakeMatrixClient`` satisfies :class:`MatrixClientProtocol` in memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class _StoredEvent:
    event_id: str
    event_type: str
    sender: str
    state_key: str | None
    content: dict[str, Any]


@dataclass
class _Room:
    room_id: str
    name: str
    topic: str
    invited: list[str] = field(default_factory=list)
    state: dict[tuple[str, str], _StoredEvent] = field(default_factory=dict)
    timeline: list[_StoredEvent] = field(default_factory=list)


class FakeMatrixClient:
    """In-memory MatrixClientProtocol implementation for tests."""

    def __init__(self, user_id: str = "@tester:agora.local", homeserver_name: str = "agora.local") -> None:
        self.user_id = user_id
        self.homeserver_name = homeserver_name
        self.rooms: dict[str, _Room] = {}
        self.uploads: list[tuple[str, str]] = []
        self.logged_in = False
        self._sync_queue: list[tuple[str, dict[str, Any]]] = []
        self._sync_cursor = 0

    # ---- protocol surface ----
    async def login(self, password: str) -> None:  # noqa: ARG002
        self.logged_in = True

    async def create_room(
        self,
        name: str,
        topic: str = "",
        invite: list[str] | None = None,
        initial_state: list[dict[str, Any]] | None = None,
    ) -> str:
        room_id = f"!{uuid.uuid4().hex[:16]}:{self.homeserver_name}"
        room = _Room(room_id=room_id, name=name, topic=topic, invited=list(invite or []))
        # Seed creator membership + name/topic as state events for realism.
        self._put_state(room, "m.room.name", "", {"name": name})
        if topic:
            self._put_state(room, "m.room.topic", "", {"topic": topic})
        for ev in initial_state or []:
            self._put_state(
                room,
                ev["type"],
                ev.get("state_key", ""),
                dict(ev.get("content", {})),
            )
        self.rooms[room_id] = room
        return room_id

    async def send_state_event(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> str:
        room = self._room(room_id)
        return self._put_state(room, event_type, state_key, dict(content))

    async def send_event(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
    ) -> str:
        room = self._room(room_id)
        event_id = f"${uuid.uuid4().hex[:16]}"
        room.timeline.append(
            _StoredEvent(
                event_id=event_id,
                event_type=event_type,
                sender=self.user_id,
                state_key=None,
                content=dict(content),
            )
        )
        # Mirror to the sync queue so SyncService subscribers see every post.
        self._sync_queue.append(
            (
                room_id,
                {
                    "type": event_type,
                    "event_id": event_id,
                    "sender": self.user_id,
                    "content": dict(content),
                },
            )
        )
        return event_id

    async def get_room_state(self, room_id: str) -> list[dict[str, Any]]:
        room = self._room(room_id)
        return [self._event_to_dict(ev) for ev in room.state.values()]

    async def get_room_timeline(
        self,
        room_id: str,
        limit: int = 100,
        since: str | None = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        room = self._room(room_id)
        return [self._event_to_dict(ev) for ev in room.timeline[:limit]]

    async def upload_file(self, file_path: str) -> str:
        mxc = f"mxc://{self.homeserver_name}/{uuid.uuid4().hex[:24]}"
        self.uploads.append((file_path, mxc))
        return mxc

    async def download_file(self, mxc_uri: str, dest_dir: str) -> str:
        """Resolve an MXC URI by looking up the original path from uploads."""
        import shutil
        from pathlib import Path

        for original_path, uri in self.uploads:
            if uri == mxc_uri:
                dest = Path(dest_dir)
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f"kb-{Path(original_path).name}"
                shutil.copy2(original_path, target)
                return str(target)
        raise KeyError(f"no upload found for {mxc_uri}")

    async def sync_once(
        self,
        timeout_ms: int = 30000,  # noqa: ARG002
        since: str | None = None,
        rooms: list[str] | None = None,
    ):
        """Drain queued events into a SyncBatch. ``since`` is an integer cursor."""
        from agora.matrix.client import SyncBatch

        start = int(since) if since and since.isdigit() else 0
        queued = self._sync_queue[start:]
        if rooms:
            watched = set(rooms)
            queued = [(r, e) for r, e in queued if r in watched]
        self._sync_cursor = len(self._sync_queue)
        return SyncBatch(events=list(queued), next_since=str(self._sync_cursor))

    def queue_event(self, room_id: str, event: dict[str, Any]) -> None:
        """Append an event to the sync queue. Auto-fills event_id if missing."""
        ev = dict(event)
        ev.setdefault("event_id", f"${uuid.uuid4().hex[:16]}")
        ev.setdefault("sender", self.user_id)
        ev.setdefault("content", {})
        self._sync_queue.append((room_id, ev))

    async def close(self) -> None:
        self.logged_in = False

    # ---- helpers ----
    def _room(self, room_id: str) -> _Room:
        if room_id not in self.rooms:
            raise KeyError(f"unknown room {room_id}")
        return self.rooms[room_id]

    def _put_state(self, room: _Room, etype: str, state_key: str, content: dict[str, Any]) -> str:
        event_id = f"${uuid.uuid4().hex[:16]}"
        room.state[(etype, state_key)] = _StoredEvent(
            event_id=event_id,
            event_type=etype,
            sender=self.user_id,
            state_key=state_key,
            content=content,
        )
        return event_id

    @staticmethod
    def _event_to_dict(ev: _StoredEvent) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": ev.event_type,
            "event_id": ev.event_id,
            "sender": ev.sender,
            "content": ev.content,
        }
        if ev.state_key is not None:
            base["state_key"] = ev.state_key
        return base


@pytest.fixture
def fake_matrix_client() -> FakeMatrixClient:
    return FakeMatrixClient()


# ===================================================================================
# FakeLLM — scripted LLM for fleet tests
# ===================================================================================

from collections.abc import Callable  # noqa: E402

from agora.fleet.llm_adapter import LLMResponse, ToolCall  # noqa: E402

ResponsePlan = list[LLMResponse] | Callable[[list[dict[str, Any]]], LLMResponse]


class FakeLLM:
    """Replays a scripted sequence of responses, or defers to a callable.

    Usage in tests::

        llm = FakeLLM([
            LLMResponse(content="", tool_calls=(ToolCall("id1", "write_file", {...}),)),
            LLMResponse(content="done", tool_calls=()),
            LLMResponse(content='[{"category":"pattern","content":"x","confidence":0.7}]'),
        ])
    """

    def __init__(self, plan: ResponsePlan) -> None:
        self._plan = plan
        self._idx = 0
        self.calls: list[dict[str, Any]] = []

    # Block-array shaping (one dict per turn) so FakeLLM satisfies LLMProtocol's
    # optional format hooks. Kept inline (not imported from an adapter) so the
    # fake stays self-contained.
    def format_assistant_turn(self, response):
        blocks: list[dict[str, Any]] = []
        if response.content:
            blocks.append({"type": "text", "text": response.content})
        for call in response.tool_calls:
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return {"role": "assistant", "content": blocks}

    def format_tool_results(self, calls, results):
        blocks = [
            {"type": "tool_result", "tool_use_id": call.id, "content": result}
            for call, result in zip(calls, results, strict=True)
        ]
        return {"role": "user", "content": blocks}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.calls.append(
            {"messages": list(messages), "system": system, "tools": tools, "model": model}
        )
        if callable(self._plan):
            return self._plan(messages)
        if self._idx >= len(self._plan):
            return LLMResponse(content="", tool_calls=(), usage={})
        resp = self._plan[self._idx]
        self._idx += 1
        return resp


def tool_call(name: str, arguments: dict[str, Any], tid: str | None = None) -> ToolCall:
    import uuid as _uuid

    return ToolCall(id=tid or _uuid.uuid4().hex[:8], name=name, arguments=arguments)
