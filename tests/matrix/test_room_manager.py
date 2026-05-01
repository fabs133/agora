from pathlib import Path

import pytest

from agora.core.agent import AgentConfig
from agora.core.errors import AgoraError
from agora.core.learning import Learning
from agora.core.types import AgentRole, LearningCategory
from agora.matrix.events import (
    AGENT_CONFIG_EVENT,
    KNOWLEDGE_REF_EVENT,
    LEARNING_EVENT,
    learning_to_content,
)
from agora.matrix.room_manager import RoomManager


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(
        name="architect",
        role=AgentRole.ARCHITECT,
        instructions="design things carefully",
    )


async def test_create_identity_room_sets_state(fake_matrix_client, config) -> None:
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")
    room_id, agent_id = await mgr.create_identity_room(config)

    assert agent_id == "@architect:agora.local"
    state = await fake_matrix_client.get_room_state(room_id)
    types = {ev["type"] for ev in state}
    assert AGENT_CONFIG_EVENT in types
    cfg_event = next(ev for ev in state if ev["type"] == AGENT_CONFIG_EVENT)
    assert cfg_event["content"]["name"] == "architect"
    assert cfg_event["content"]["role"] == "architect"


async def test_create_project_room_invites_agents(fake_matrix_client) -> None:
    mgr = RoomManager(fake_matrix_client)
    room_id = await mgr.create_project_room("demo", ["@a:agora.local", "@b:agora.local"])
    room = fake_matrix_client.rooms[room_id]
    assert room.invited == ["@a:agora.local", "@b:agora.local"]
    assert "demo" in room.name


async def test_hydrate_identity_loads_config_and_learnings(fake_matrix_client, config) -> None:
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")
    room_id, _ = await mgr.create_identity_room(config)

    # Append a learning to the timeline.
    learning = Learning(
        id="l1",
        category=LearningCategory.PATTERN,
        content="prefer immutability",
        confidence=0.8,
        task_ref="t0",
    )
    await fake_matrix_client.send_event(room_id, LEARNING_EVENT, learning_to_content(learning))

    identity = await mgr.hydrate_identity(room_id)
    assert identity.config == config
    assert identity.agent_id == "@architect:agora.local"
    assert len(identity.learned_patterns) == 1
    assert identity.learned_patterns[0].content == "prefer immutability"


async def test_hydrate_identity_drops_malformed_learning(fake_matrix_client, config) -> None:
    mgr = RoomManager(fake_matrix_client)
    room_id, _ = await mgr.create_identity_room(config)

    # Valid one...
    await fake_matrix_client.send_event(
        room_id,
        LEARNING_EVENT,
        learning_to_content(
            Learning(
                id="ok",
                category=LearningCategory.PATTERN,
                content="good",
                confidence=0.7,
                task_ref="t1",
            )
        ),
    )
    # ...plus a malformed one (missing fields).
    await fake_matrix_client.send_event(room_id, LEARNING_EVENT, {"id": "bad"})

    identity = await mgr.hydrate_identity(room_id)
    contents = [l.content for l in identity.learned_patterns]
    assert contents == ["good"]


async def test_hydrate_identity_raises_without_config(fake_matrix_client) -> None:
    mgr = RoomManager(fake_matrix_client)
    room_id = await fake_matrix_client.create_room("empty")
    with pytest.raises(AgoraError, match="no m.agora.agent_config"):
        await mgr.hydrate_identity(room_id)


async def test_upload_knowledge_stores_mxc_ref(
    fake_matrix_client, config, tmp_path: Path
) -> None:
    mgr = RoomManager(fake_matrix_client)
    room_id, _ = await mgr.create_identity_room(config)

    f = tmp_path / "notes.md"
    f.write_text("# notes", encoding="utf-8")
    mxc = await mgr.upload_knowledge(room_id, str(f), description="design notes")

    assert mxc.startswith("mxc://")
    state = await fake_matrix_client.get_room_state(room_id)
    refs = [ev for ev in state if ev["type"] == KNOWLEDGE_REF_EVENT]
    assert len(refs) == 1
    assert refs[0]["content"]["mxc_uri"] == mxc
    assert refs[0]["content"]["filename"] == "notes.md"
