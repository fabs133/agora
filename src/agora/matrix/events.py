"""Custom Matrix event types for Agora (``m.agora.*`` namespace).

Each event type has a pair of helpers:
- ``<model>_to_content(obj) -> dict`` — produce the Matrix event ``content`` payload
- ``<model>_from_content(content) -> obj`` — parse an event content dict back to a model

Validation raises :class:`~agora.core.errors.AgoraError` on malformed content.
All timestamps use ISO-8601 strings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agora.core.agent import DEFAULT_MODEL, AgentConfig
from agora.core.errors import AgoraError
from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import (
    AgentRole,
    LearningCategory,
    ProjectPhase,
    TaskId,
    TaskStatus,
)

# --------------------------------- Event types ---------------------------------

AGENT_CONFIG_EVENT = "m.agora.agent_config"
TASK_EVENT = "m.agora.task"
TASK_RESULT_EVENT = "m.agora.task_result"
CONTRACT_EVENT = "m.agora.contract"
LEARNING_EVENT = "m.agora.learning"
PHASE_CHANGE_EVENT = "m.agora.phase_change"
KNOWLEDGE_REF_EVENT = "m.agora.knowledge_ref"

_SCHEMA_VERSION = 1


def _require(content: dict[str, Any], key: str, event_type: str) -> Any:
    if key not in content:
        raise AgoraError(f"{event_type} event missing required field '{key}'")
    return content[key]


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ------------------------------ m.agora.agent_config ------------------------------


def agent_config_to_content(config: AgentConfig) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "name": config.name,
        "role": config.role.value,
        "model": config.model,
        "instructions": config.instructions,
        "knowledge_files": list(config.knowledge_files),
    }


def agent_config_from_content(content: dict[str, Any]) -> AgentConfig:
    try:
        return AgentConfig(
            name=_require(content, "name", AGENT_CONFIG_EVENT),
            role=AgentRole(_require(content, "role", AGENT_CONFIG_EVENT)),
            model=content.get("model", DEFAULT_MODEL),
            instructions=content.get("instructions", ""),
            knowledge_files=tuple(content.get("knowledge_files", [])),
        )
    except ValueError as exc:
        raise AgoraError(f"invalid agent_config content: {exc}") from exc


# --------------------------------- m.agora.task ---------------------------------


def task_to_content(
    *,
    task_id: TaskId,
    description: str,
    agent_id: str | None,
    status: TaskStatus,
    fingerprint: str,
    depends_on: tuple[TaskId, ...] = (),
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "task_id": task_id,
        "description": description,
        "agent_id": agent_id,
        "status": status.value,
        "fingerprint": fingerprint,
        "depends_on": list(depends_on),
        "timestamp": _now(),
    }


def task_from_content(content: dict[str, Any]) -> dict[str, Any]:
    """Parse a task event into a dict (orchestrator hydrates into Task).

    We return a dict, not a Task, because Task requires a ``Specification`` whose
    predicate callables do not survive serialization. The orchestrator reconstructs
    the Specification from its contract event (stored separately).
    """
    try:
        return {
            "task_id": _require(content, "task_id", TASK_EVENT),
            "description": content.get("description", ""),
            "agent_id": content.get("agent_id"),
            "status": TaskStatus(_require(content, "status", TASK_EVENT)),
            "fingerprint": _require(content, "fingerprint", TASK_EVENT),
            "depends_on": tuple(content.get("depends_on", [])),
            "timestamp": content.get("timestamp", ""),
        }
    except ValueError as exc:
        raise AgoraError(f"invalid task content: {exc}") from exc


# ------------------------------ m.agora.task_result ------------------------------


def task_result_to_content(
    *,
    task_id: TaskId,
    success: bool,
    output: str,
    artifacts: list[str],
    postcondition_results: list[tuple[str, bool, str]],
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "task_id": task_id,
        "success": success,
        "output": output,
        "artifacts": list(artifacts),
        "postcondition_results": [
            {"name": n, "passed": p, "reason": r} for n, p, r in postcondition_results
        ],
        "timestamp": _now(),
    }


def task_result_from_content(content: dict[str, Any]) -> dict[str, Any]:
    _require(content, "task_id", TASK_RESULT_EVENT)
    _require(content, "success", TASK_RESULT_EVENT)
    return {
        "task_id": content["task_id"],
        "success": bool(content["success"]),
        "output": content.get("output", ""),
        "artifacts": list(content.get("artifacts", [])),
        "postcondition_results": [
            (r["name"], bool(r["passed"]), r.get("reason", ""))
            for r in content.get("postcondition_results", [])
        ],
        "timestamp": content.get("timestamp", ""),
    }


# ------------------------------- m.agora.contract -------------------------------


def contract_to_content(
    *,
    fingerprint: str,
    description: str,
    precondition_descriptions: list[str],
    postcondition_descriptions: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "description": description,
        "preconditions": list(precondition_descriptions),
        "postconditions": list(postcondition_descriptions),
    }


def contract_from_content(content: dict[str, Any]) -> dict[str, Any]:
    _require(content, "fingerprint", CONTRACT_EVENT)
    return {
        "fingerprint": content["fingerprint"],
        "description": content.get("description", ""),
        "preconditions": list(content.get("preconditions", [])),
        "postconditions": list(content.get("postconditions", [])),
    }


# ------------------------------- m.agora.learning -------------------------------


# Matrix canonical JSON (spec §10) forbids floats in event content — Conduit
# enforces this strictly (panics with "IntConvert"). We encode confidence as
# an integer in basis points (0-10000) and decode on read. Precision is 0.0001.
_CONFIDENCE_SCALE = 10_000


def learning_to_content(learning: Learning) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "id": learning.id,
        "category": learning.category.value,
        "content": learning.content,
        "confidence_bp": int(round(max(0.0, min(1.0, learning.confidence)) * _CONFIDENCE_SCALE)),
        "task_ref": learning.task_ref,
        "reinforcement_count": learning.reinforcement_count,
        "created_at": learning.created_at,
        "last_reinforced_at": learning.last_reinforced_at,
    }


def learning_from_content(content: dict[str, Any]) -> Learning:
    try:
        # New events use confidence_bp (int basis points); old events may still
        # carry confidence (float) — accept both for forward/backward compat.
        if "confidence_bp" in content:
            confidence = float(int(content["confidence_bp"])) / _CONFIDENCE_SCALE
        elif "confidence" in content:
            confidence = float(content["confidence"])
        else:
            raise AgoraError(
                f"{LEARNING_EVENT} missing required field 'confidence_bp'"
            )
        return Learning(
            id=_require(content, "id", LEARNING_EVENT),
            category=LearningCategory(_require(content, "category", LEARNING_EVENT)),
            content=_require(content, "content", LEARNING_EVENT),
            confidence=confidence,
            task_ref=_require(content, "task_ref", LEARNING_EVENT),
            reinforcement_count=int(content.get("reinforcement_count", 0)),
            created_at=content.get("created_at", ""),
            last_reinforced_at=content.get("last_reinforced_at", ""),
        )
    except (ValueError, TypeError) as exc:
        raise AgoraError(f"invalid learning content: {exc}") from exc


# ----------------------------- m.agora.phase_change -----------------------------


def phase_change_to_content(change: PhaseChange) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "from_phase": change.from_phase.value,
        "to_phase": change.to_phase.value,
        "reason": change.reason,
        "timestamp": change.timestamp,
    }


def phase_change_from_content(content: dict[str, Any]) -> PhaseChange:
    try:
        return PhaseChange(
            from_phase=ProjectPhase(_require(content, "from_phase", PHASE_CHANGE_EVENT)),
            to_phase=ProjectPhase(_require(content, "to_phase", PHASE_CHANGE_EVENT)),
            reason=content.get("reason", ""),
            timestamp=content.get("timestamp", _now()),
        )
    except ValueError as exc:
        raise AgoraError(f"invalid phase_change content: {exc}") from exc


# ----------------------------- m.agora.knowledge_ref -----------------------------


def knowledge_ref_to_content(*, mxc_uri: str, filename: str, description: str = "") -> dict[str, Any]:
    if not mxc_uri.startswith("mxc://"):
        raise AgoraError(f"knowledge_ref mxc_uri must start with 'mxc://': got {mxc_uri!r}")
    return {
        "schema_version": _SCHEMA_VERSION,
        "mxc_uri": mxc_uri,
        "filename": filename,
        "description": description,
    }


def knowledge_ref_from_content(content: dict[str, Any]) -> dict[str, Any]:
    uri = _require(content, "mxc_uri", KNOWLEDGE_REF_EVENT)
    if not str(uri).startswith("mxc://"):
        raise AgoraError(f"knowledge_ref mxc_uri invalid: {uri!r}")
    return {
        "mxc_uri": uri,
        "filename": _require(content, "filename", KNOWLEDGE_REF_EVENT),
        "description": content.get("description", ""),
    }


# -------------------------------- Event registry --------------------------------

AGORA_EVENT_TYPES: frozenset[str] = frozenset(
    {
        AGENT_CONFIG_EVENT,
        TASK_EVENT,
        TASK_RESULT_EVENT,
        CONTRACT_EVENT,
        LEARNING_EVENT,
        PHASE_CHANGE_EVENT,
        KNOWLEDGE_REF_EVENT,
    }
)


def is_agora_event(event_type: str) -> bool:
    return event_type in AGORA_EVENT_TYPES
