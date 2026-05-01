"""Live integration test against a real Conduit homeserver.

Skipped by default. Enable by setting ``AGORA_MATRIX_LIVE=1`` and providing a
homeserver URL + registration token via env vars. The test registers a throwaway
user, creates a room, writes state, reads it back, then closes the client.
"""

from __future__ import annotations

import os
import uuid

import aiohttp
import pytest

from agora.matrix.client import AgoraMatrixClient
from agora.matrix.events import AGENT_CONFIG_EVENT

pytestmark = pytest.mark.skipif(
    os.getenv("AGORA_MATRIX_LIVE") != "1",
    reason="live Matrix test — set AGORA_MATRIX_LIVE=1 to run",
)


async def _register(homeserver: str, username: str, password: str, token: str) -> None:
    url = f"{homeserver}/_matrix/client/v3/register"
    payload = {
        "auth": {"type": "m.login.registration_token", "token": token},
        "username": username,
        "password": password,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status in (200, 400):  # 400 = user_in_use on re-run
                return
            text = await resp.text()
            raise RuntimeError(f"register failed ({resp.status}): {text}")


async def test_live_smoke() -> None:
    homeserver = os.getenv("AGORA_MATRIX_HOMESERVER", "http://localhost:6167")
    token = os.getenv("AGORA_MATRIX_TOKEN", "dev_only_CHANGE_ME")
    homeserver_name = os.getenv("AGORA_MATRIX_SERVER_NAME", "agora.local")

    username = f"smoke_{uuid.uuid4().hex[:8]}"
    password = "smoke-pass-1234"
    await _register(homeserver, username, password, token)

    client = AgoraMatrixClient(homeserver=homeserver, user_id=f"@{username}:{homeserver_name}")
    await client.login(password)
    try:
        room_id = await client.create_room(
            name="smoke",
            topic="smoke test",
            initial_state=[
                {
                    "type": AGENT_CONFIG_EVENT,
                    "state_key": "",
                    "content": {"name": username, "role": "architect"},
                }
            ],
        )
        state = await client.get_room_state(room_id)
        assert any(ev.get("type") == AGENT_CONFIG_EVENT for ev in state)
    finally:
        await client.close()
