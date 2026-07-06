"""Role write-scope rules — the single source of truth for who may write where.

Used by two call sites so they can never drift:
  - :func:`agora.fleet.inner_tools._enforce_path_scope` (runtime write guard).
  - flow validation (:func:`agora.core.flow.load_flow`) — a load-time lint that
    refuses a flow whose task is assigned to a role that cannot write its own
    declared output (the integration-run-1 T5.1 bug: an implementer task whose
    output_path was ``tests/test_core.py``, silently rejected 17× at runtime).

Scope rules (v2.7):
  - ``implementer`` may not write ``tests/**`` (tests are the tester's turf).
  - ``tester`` may not write ``src/**`` (implementation is the implementer's).
  - ``architect`` / ``reviewer`` / unknown: unrestricted.
"""

from __future__ import annotations

from agora.core.types import AgentRole


def role_can_write(role: AgentRole, rel: str) -> bool:
    """True iff ``role`` is permitted to write the work_dir-relative path ``rel``."""
    rel_norm = (rel or "").replace("\\", "/").lstrip("/")
    if role is AgentRole.IMPLEMENTER and rel_norm.startswith("tests/"):
        return False
    if role is AgentRole.TESTER and rel_norm.startswith("src/"):
        return False
    return True


def write_scope_reason(role: AgentRole, rel: str) -> str:
    """Short reason a write is out of scope (empty when it is allowed)."""
    if role_can_write(role, rel):
        return ""
    if role is AgentRole.IMPLEMENTER:
        return f"implementer role may not write {rel!r} — tests/ is owned by the tester"
    return f"tester role may not write {rel!r} — src/ is owned by the implementer"


__all__ = ["role_can_write", "write_scope_reason"]
