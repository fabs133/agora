"""Role contracts loader (roles.yaml) — capability-program L1-C.

A ROLE is a stable contract: what the seat REQUIRES, independent of who sits in
it. Requirements reference capability-matrix columns (sub_targets), so
``agora cast eligible <role>`` can query the matrix for models that satisfy them
and ``validate_cast`` can check that a binding's cited matrix evidence actually
does. See docs/design/roles-and-casting.md.

``requires`` is either a :class:`RoleRequirement` (measured: a probe + minimum
thresholds) or a sentinel string (``"unmeasured"`` / ``"task_specific"``) — a
role with no measured basis can still be cast, but only with a waiver or a human
binding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from agora.core.errors import AgoraError

#: Special key inside ``requires.min``: the max number of DISTINCT trajectory
#: signatures allowed across repeats. Value 1 ⇒ full determinism ⇒
#: trajectory_reproducibility_rate must be 1.0. It is NOT a sub_target threshold.
REPEAT_DISTINCT_MAX = "repeat_distinct_max"


class RoleRequirement(BaseModel):
    """A measured requirement: a probe plus per-sub_target minimum thresholds.

    ``min`` maps a capability-vector ``sub_target`` (e.g. ``pass_rate``) to the
    minimum value a candidate must reach. The special key
    :data:`REPEAT_DISTINCT_MAX` is a determinism cap, handled separately from the
    sub_target thresholds by the eligibility check.
    """

    model_config = {"extra": "forbid"}

    probe: str
    min: dict[str, float] = Field(default_factory=dict)

    @property
    def sub_target_minimums(self) -> dict[str, float]:
        """The ``min`` entries that are genuine sub_target thresholds (drops the
        determinism cap, which the eligibility check translates separately)."""
        return {k: v for k, v in self.min.items() if k != REPEAT_DISTINCT_MAX}

    @property
    def repeat_distinct_max(self) -> int | None:
        raw = self.min.get(REPEAT_DISTINCT_MAX)
        return int(raw) if raw is not None else None


class Role(BaseModel):
    """One role contract. ``harness`` is the harness the role runs under — its
    :func:`~agora.bench.keys.harness_hash` is the matrix key a candidate must be
    measured at (a model measured under a different harness is not evidence for
    this seat)."""

    model_config = {"extra": "forbid"}

    contract: str = ""
    harness: dict[str, Any] = Field(default_factory=dict)
    requires: RoleRequirement | str
    prefer: dict[str, Any] | None = None

    @property
    def measured(self) -> RoleRequirement | None:
        """The requirement when the role is measured, else None (sentinel)."""
        return self.requires if isinstance(self.requires, RoleRequirement) else None


class RoleSet(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: int = 1
    roles: dict[str, Role]

    def role(self, name: str) -> Role:
        if name not in self.roles:
            raise AgoraError(f"unknown role {name!r}; known: {sorted(self.roles)}")
        return self.roles[name]


def load_roles(path: str | Path = "roles.yaml") -> RoleSet:
    """Load + validate roles.yaml. Raises :class:`AgoraError` on a missing file
    or schema mismatch (at LOAD time)."""
    p = Path(path)
    if not p.is_file():
        raise AgoraError(f"roles file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AgoraError(f"roles at {p} could not be parsed as YAML: {exc}") from exc
    try:
        return RoleSet.model_validate(raw)
    except ValidationError as exc:
        raise AgoraError(f"roles at {p} failed schema validation:\n{exc}") from exc


__all__ = ["REPEAT_DISTINCT_MAX", "Role", "RoleRequirement", "RoleSet", "load_roles"]
