"""Shared enums and type aliases used across the core domain."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


class AgentRole(str, Enum):
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    TESTER = "tester"


class ProjectPhase(str, Enum):
    INIT = "init"
    ANALYSIS = "analysis"
    ARCHITECTURE = "architecture"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


class LearningCategory(str, Enum):
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
