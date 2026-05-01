"""Agent identity models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agora.core.types import AgentId, AgentRole, RoomId

if TYPE_CHECKING:
    from agora.core.learning import Learning

DEFAULT_MODEL = "claude-sonnet-4-20250514"


@dataclass(frozen=True)
class AgentConfig:
    """Configuration needed to create a new agent."""

    name: str
    role: AgentRole
    model: str = DEFAULT_MODEL
    instructions: str = ""
    knowledge_files: tuple[str, ...] = ()


@dataclass
class AgentIdentity:
    """Full identity hydrated from a Matrix identity room (in later sprints)."""

    agent_id: AgentId
    room_id: RoomId
    config: AgentConfig
    knowledge_refs: list[str] = field(default_factory=list)
    learned_patterns: list["Learning"] = field(default_factory=list)

    @property
    def effective_instructions(self) -> str:
        """Base instructions plus any active learnings above the confidence threshold."""
        from agora.core.learning import filter_active, format_learnings_for_context

        base = self.config.instructions.strip()
        active = filter_active(self.learned_patterns)
        if not active:
            return base
        block = format_learnings_for_context(active)
        return f"{base}\n\n{block}".strip() if base else block
