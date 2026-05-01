"""Room lifecycle: identity rooms and project coordination rooms."""

from __future__ import annotations

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.errors import AgoraError
from agora.core.types import AgentId, RoomId
from agora.matrix.client import MatrixClientProtocol
from agora.matrix.events import (
    AGENT_CONFIG_EVENT,
    KNOWLEDGE_REF_EVENT,
    LEARNING_EVENT,
    agent_config_from_content,
    agent_config_to_content,
    knowledge_ref_to_content,
    learning_from_content,
)


class RoomManager:
    """Creates and hydrates identity + project rooms."""

    def __init__(self, client: MatrixClientProtocol, homeserver_name: str = "agora.local") -> None:
        self._client = client
        self._homeserver = homeserver_name

    async def create_identity_room(self, config: AgentConfig) -> tuple[RoomId, AgentId]:
        """Create an identity room with ``m.agora.agent_config`` as initial state."""
        initial_state = [
            {
                "type": AGENT_CONFIG_EVENT,
                "state_key": "",
                "content": agent_config_to_content(config),
            }
        ]
        room_id = await self._client.create_room(
            name=f"agent:{config.name}",
            topic=f"{config.role.value}: {config.name}",
            initial_state=initial_state,
        )
        agent_id = f"@{config.name}:{self._homeserver}"
        return room_id, agent_id

    async def create_project_room(
        self, project_name: str, agent_ids: list[AgentId]
    ) -> RoomId:
        return await self._client.create_room(
            name=f"project:{project_name}",
            topic=f"Agora project coordination — {project_name}",
            invite=list(agent_ids),
        )

    async def hydrate_identity(self, room_id: RoomId) -> AgentIdentity:
        """Read room state + timeline to rebuild an :class:`AgentIdentity`."""
        state = await self._client.get_room_state(room_id)

        config: AgentConfig | None = None
        knowledge_refs: list[str] = []
        for event in state:
            etype = event.get("type")
            content = event.get("content") or {}
            if etype == AGENT_CONFIG_EVENT and config is None:
                config = agent_config_from_content(content)
            elif etype == KNOWLEDGE_REF_EVENT:
                uri = content.get("mxc_uri")
                if uri:
                    knowledge_refs.append(uri)

        if config is None:
            raise AgoraError(
                f"room {room_id} has no {AGENT_CONFIG_EVENT} state event — not an identity room"
            )

        timeline = await self._client.get_room_timeline(room_id, limit=500)
        learnings = []
        for event in timeline:
            if event.get("type") == LEARNING_EVENT:
                try:
                    learnings.append(learning_from_content(event.get("content") or {}))
                except AgoraError:
                    # Skip malformed entries rather than failing hydration entirely.
                    continue

        agent_id = f"@{config.name}:{self._homeserver}"
        return AgentIdentity(
            agent_id=agent_id,
            room_id=room_id,
            config=config,
            knowledge_refs=knowledge_refs,
            learned_patterns=learnings,
        )

    async def upload_knowledge(
        self,
        room_id: RoomId,
        file_path: str,
        description: str = "",
    ) -> str:
        """Upload a file and record its MXC URI as a ``m.agora.knowledge_ref`` state event."""
        mxc = await self._client.upload_file(file_path)
        filename = file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        await self._client.send_state_event(
            room_id=room_id,
            event_type=KNOWLEDGE_REF_EVENT,
            content=knowledge_ref_to_content(
                mxc_uri=mxc, filename=filename, description=description
            ),
            state_key=mxc,
        )
        return mxc
