"""Role write-scope rules — the single source of truth for who may write where.

Position: the write-permission oracle both the runtime guard and flow validation
consult. Deliberately import-light (only :class:`AgentRole`) so it can be called
from either layer without a cycle.

Invariant (why it exists as one module): two independent call sites must apply
the SAME rule, or a flow that lints clean can still be rejected at runtime —
    - :func:`agora.fleet.inner_tools._enforce_path_scope` (runtime write guard);
    - flow validation (:func:`agora.core.flow.load_flow`), a load-time lint that
      refuses a task assigned to a role that cannot write its own declared output.

Findings this encodes:
  - **F1** — two role systems collided: this v2.7 "turf" rule (legacy
    architect/implementer/tester/reviewer pipeline) silently rejected all 17
    write_file calls of the integration-run-1 T5.1 task, whose output_path was
    ``tests/test_core.py`` — an implementer writing the tester's turf.
  - **F2** — that feasibility (can this role write its declared output?) is
    statically checkable, so the collision is now caught at LOAD time by the
    lint above rather than as invisible runtime rejections.

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
