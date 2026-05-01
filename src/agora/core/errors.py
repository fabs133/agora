"""Domain exception hierarchy. All Agora errors descend from AgoraError."""

from __future__ import annotations

from typing import Any


class AgoraError(Exception):
    """Base exception for all Agora domain errors."""


class ContractViolation(AgoraError):
    """A pre- or postcondition was not satisfied."""

    def __init__(self, condition: str, context: dict[str, Any] | None = None) -> None:
        self.condition = condition
        self.context = context or {}
        super().__init__(f"Contract violated: {condition}")


class InvalidTransition(AgoraError):
    """Attempted an invalid state transition."""

    def __init__(self, from_state: str, to_state: str, reason: str = "") -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        msg = f"Invalid transition {from_state} -> {to_state}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


class TaskError(AgoraError):
    """Error during task execution or construction."""


class AgentError(AgoraError):
    """Error related to agent configuration or runtime."""


class DuplicateFingerprint(AgoraError):
    """A task with this fingerprint was already processed (retry dedup)."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        super().__init__(f"Duplicate fingerprint: {fingerprint}")
