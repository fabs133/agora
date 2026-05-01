"""Shared enums and type aliases used across the core domain."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """Task lifecycle states. Transitions are gated by ``VALID_TASK_TRANSITIONS``
    in :mod:`agora.core.task` — ``DONE`` is terminal, ``FAILED`` is recoverable
    by transitioning back to ``PENDING`` for retry."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


class AgentRole(str, Enum):
    """The four roles the framework supports. Each carries its own system-prompt
    template; see :class:`agora.core.agent.AgentIdentity`. Roles are descriptive
    not enforced — an agent of any role can be assigned any task by the
    dispatcher."""

    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    TESTER = "tester"


class ProjectPhase(str, Enum):
    """Project-level state machine phases. Transitions are gated by
    :data:`agora.core.project.VALID_TRANSITIONS`. ``REVIEW`` can route back to
    ``ANALYSIS`` / ``ARCHITECTURE`` / ``IMPLEMENTATION`` (the loopback paths)
    or forward to ``DONE``; ``FAILED`` is reachable from any non-terminal phase
    on unrecoverable error."""

    INIT = "init"
    ANALYSIS = "analysis"
    ARCHITECTURE = "architecture"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


class LearningCategory(str, Enum):
    """How a :class:`~agora.core.learning.Learning` is grouped in the
    "Learned context" prompt block. ``FAILURE`` is the most common — it's what
    auto-learnings synthesise from postcondition failures; the others are
    reserved for human-authored or higher-level patterns."""

    PATTERN = "pattern"
    FAILURE = "failure"
    PREFERENCE = "preference"
    TOOL_USAGE = "tool_usage"


RoomId = str
EventId = str
AgentId = str
TaskId = str
ProjectId = str
Fingerprint = str
