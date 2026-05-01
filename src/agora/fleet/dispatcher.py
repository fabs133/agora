"""Task → agent assignment.

Simple heuristics: match by explicit name, by role, or pick the least-loaded
agent with a matching role. Assignment tracking is in-memory — the orchestrator
feeds back completion so loads stay current.
"""

from __future__ import annotations

from collections import Counter

from agora.core.agent import AgentIdentity
from agora.core.errors import AgentError
from agora.core.task import Task
from agora.core.types import AgentRole


class Dispatcher:
    def __init__(self, agents: list[AgentIdentity]) -> None:
        if not agents:
            raise AgentError("dispatcher requires at least one agent")
        self._agents = list(agents)
        self._load: Counter[str] = Counter()

    @property
    def agents(self) -> list[AgentIdentity]:
        return list(self._agents)

    def assign(self, task: Task) -> AgentIdentity:
        """Resolve a task to an agent.

        Preference order:
        1. ``task.agent_id`` set and matches an agent name → that agent.
        2. ``task.agent_id`` matches a role name → least-loaded agent with that role.
        3. No hint → the single agent (if only one); otherwise raise.
        """
        if task.agent_id:
            for agent in self._agents:
                if agent.config.name == task.agent_id:
                    self._bump(agent)
                    return agent
            try:
                role = AgentRole(task.agent_id)
            except ValueError:
                pass
            else:
                return self.assign_by_role(task, role)
            raise AgentError(f"no agent matches task.agent_id={task.agent_id!r}")

        if len(self._agents) == 1:
            agent = self._agents[0]
            self._bump(agent)
            return agent
        raise AgentError(f"task {task.id} has no agent_id and multiple agents exist")

    def assign_by_name(self, task: Task, agent_name: str) -> AgentIdentity:
        for agent in self._agents:
            if agent.config.name == agent_name:
                self._bump(agent)
                return agent
        raise AgentError(f"no agent named {agent_name!r}")

    def assign_by_role(self, task: Task, role: AgentRole) -> AgentIdentity:
        candidates = [a for a in self._agents if a.config.role == role]
        if not candidates:
            raise AgentError(f"no agent with role {role.value!r}")
        candidates.sort(key=lambda a: self._load[a.config.name])
        chosen = candidates[0]
        self._bump(chosen)
        return chosen

    def release(self, agent: AgentIdentity) -> None:
        """Call when a task finishes so load is decremented."""
        if self._load[agent.config.name] > 0:
            self._load[agent.config.name] -= 1

    def _bump(self, agent: AgentIdentity) -> None:
        self._load[agent.config.name] += 1
