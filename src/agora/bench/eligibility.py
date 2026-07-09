"""Role eligibility over the capability matrix (capability-program L1-C).

A model is ELIGIBLE for a role when, at the role's harness key (so evidence from
a different harness never counts), a measurement meets every threshold the role
requires. This backs ``agora cast eligible <role>`` and the matrix-citation check
in ``validate_cast``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from agora.bench.keys import harness_hash
from agora.fleet.roles import Role, RoleRequirement

#: The sub_target whose value the ``repeat_distinct_max=1`` determinism cap maps to.
_REPRODUCIBILITY = "trajectory_reproducibility_rate"


@dataclass
class EligibilityResult:
    """One measurement of one model at the role's key, judged against the role."""

    model_digest: str
    model: str
    strategy: str | None
    date: str
    eligible: bool
    failures: list[str] = field(default_factory=list)
    rows: pd.DataFrame | None = None


def meets_requirement(measurement: pd.DataFrame, req: RoleRequirement) -> list[str]:
    """Return the list of failures (empty ⇒ the measurement satisfies ``req``).

    Every ``min`` sub_target must be present and ``>=`` its threshold; the
    ``repeat_distinct_max=1`` determinism cap requires
    ``trajectory_reproducibility_rate == 1.0``.
    """
    by_sub = {row["sub_target"]: row for _, row in measurement.iterrows()}
    failures: list[str] = []

    for sub_target, threshold in req.sub_target_minimums.items():
        row = by_sub.get(sub_target)
        if row is None:
            failures.append(f"{sub_target}: not measured")
        elif pd.isna(row["raw_value"]) or float(row["raw_value"]) < threshold:
            failures.append(f"{sub_target}={row['raw_value']} < {threshold}")

    cap = req.repeat_distinct_max
    if cap == 1:
        row = by_sub.get(_REPRODUCIBILITY)
        if row is None or pd.isna(row["raw_value"]) or float(row["raw_value"]) < 1.0:
            got = None if row is None else row["raw_value"]
            failures.append(f"repeat_distinct_max=1 needs {_REPRODUCIBILITY}=1.0 (got {got})")
    elif cap is not None:
        failures.append(f"repeat_distinct_max={cap} is not yet checkable against the matrix")

    return failures


def evaluate_role(
    matrix: pd.DataFrame, role: Role, *, probe_version: int | None = None
) -> list[EligibilityResult]:
    """Judge every measurement in the matrix at the role's harness key against the
    role's requirement. Empty when the role is unmeasured/task_specific, or when
    nothing is measured at that key.

    One result per ``(model_digest, strategy, date)`` measurement — a model may be
    measured more than once; the caller decides how to summarise (any-pass, latest).
    """
    req = role.measured
    if req is None or matrix.empty:
        return []

    key_hash = harness_hash(role.harness)
    rows = matrix[matrix["harness_hash"] == key_hash]
    if probe_version is not None:
        rows = rows[rows["probe_version"] == probe_version]
    if rows.empty:
        return []

    results: list[EligibilityResult] = []
    for (digest, strategy, date), group in rows.groupby(
        ["model_digest", "strategy", "date"], dropna=False
    ):
        failures = meets_requirement(group, req)
        results.append(
            EligibilityResult(
                model_digest=str(digest),
                model=str(group["model"].iloc[0]),
                strategy=None if pd.isna(strategy) else str(strategy),
                date=str(date),
                eligible=not failures,
                failures=failures,
                rows=group,
            )
        )
    return results


def eligible_digests(matrix: pd.DataFrame, role: Role, *, probe_version: int | None = None) -> set[str]:
    """The set of model_digests with at least one passing measurement for the role."""
    return {r.model_digest for r in evaluate_role(matrix, role, probe_version=probe_version) if r.eligible}
