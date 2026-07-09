"""The shared re-derivation validator (capability-program L2-1 — the trust core).

ONE implementation, used by the local ``agora contribute`` packager AND the
exchange CI (so the gate and the packager cannot drift). A submission is
mergeable iff this returns no problems.

The load-bearing check: the manifest's claimed vector is RE-DERIVED from the
submission's own raw records and required to match. A hand-edited number does
not survive — you would have to fabricate coherent raw records, not a value.
Environment (GPU, digest) is attestation, checked only for consistency + shape.
"""

from __future__ import annotations

import math
import re
from typing import Any

from agora.exchange.schema import Attestation, Manifest, manifest_rows_from_matrix

#: A row's comparison identity within one submission (digest/battery/probe are
#: constant across it; harness_hash + daemon + cell distinguish).
_ROW_KEY = ("harness_hash", "daemon_version", "model", "strategy", "sub_target", "date")
_EXACT_FIELDS = ("repeats", "excluded_repeats")
_FLOAT_FIELDS = ("raw_value", "normalized_score", "ci_low", "ci_high")

_DIGEST_RE = re.compile(r"^(sha256:)?[0-9a-f]{12,64}$")


def looks_like_digest(value: str) -> bool:
    return bool(_DIGEST_RE.match(value or ""))


def validate_submission(
    manifest: Manifest,
    attestation: Attestation,
    run_records: list[Any],
    task_records: list[Any],
) -> list[str]:
    """Return the list of problems (empty ⇒ the submission is valid / mergeable)."""
    problems: list[str] = []

    # 1. Key + attestation consistency.
    if manifest.model_digest != attestation.model_digest:
        problems.append(
            f"manifest model_digest {manifest.model_digest!r} != attestation "
            f"{attestation.model_digest!r}"
        )
    if manifest.battery_version != attestation.battery_version:
        problems.append(
            f"manifest battery_version {manifest.battery_version!r} != attestation "
            f"{attestation.battery_version!r}"
        )
    if not looks_like_digest(manifest.model_digest):
        problems.append(f"model_digest {manifest.model_digest!r} is not a plausible digest")
    if not run_records:
        problems.append("submission carries no run records")
        return problems

    # 2. RE-DERIVE the vector from the raw records and require a match.
    try:
        rederived = _rederive_rows(run_records, task_records, attestation)
    except Exception as exc:  # noqa: BLE001 — any derivation failure is a rejection
        problems.append(f"vector could not be re-derived from the records: {exc}")
        return problems
    problems.extend(_compare(manifest, rederived))

    # 3. Plausibility lints (cheap fabrication tells).
    problems.extend(_plausibility(run_records))
    return problems


def _rederive_rows(run_records: list[Any], task_records: list[Any], attestation: Attestation) -> dict[tuple, dict]:
    from agora.bench.matrix import derive_matrix_rows
    from agora.observe.analysis import build_runs_df, build_tasks_df

    stamped = [
        r.model_copy(
            update={
                "model_digest": r.model_digest or attestation.model_digest,
                "battery_version": r.battery_version or attestation.battery_version,
            }
        )
        for r in run_records
    ]
    runs_df = build_runs_df(stamped, campaign_name="submission")
    tasks_df = build_tasks_df(task_records, runs_df)
    df = derive_matrix_rows(stamped, tasks_df, runs_df)
    return {_key(row): row for row in manifest_rows_from_matrix(df)}


def _key(row: dict[str, Any]) -> tuple:
    return tuple(row.get(k) for k in _ROW_KEY)


def _compare(manifest: Manifest, rederived: dict[tuple, dict]) -> list[str]:
    problems: list[str] = []
    claimed = {_key(r.model_dump()): r.model_dump() for r in manifest.rows}

    missing = set(rederived) - set(claimed)
    extra = set(claimed) - set(rederived)
    if missing:
        problems.append(f"manifest is missing {len(missing)} re-derived row(s): {sorted(missing)[:3]}")
    if extra:
        problems.append(f"manifest claims {len(extra)} row(s) that do not re-derive: {sorted(extra)[:3]}")

    for k in set(claimed) & set(rederived):
        c, d = claimed[k], rederived[k]
        for f in _EXACT_FIELDS:
            if c.get(f) != d.get(f):
                problems.append(f"row {k}: {f} claimed {c.get(f)} != re-derived {d.get(f)}")
        for f in _FLOAT_FIELDS:
            if not _close(c.get(f), d.get(f)):
                problems.append(f"row {k}: {f} claimed {c.get(f)} != re-derived {d.get(f)}")
    return problems


def _close(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def _plausibility(run_records: list[Any]) -> list[str]:
    problems: list[str] = []
    for r in run_records:
        rid = getattr(r, "run_id", "?")
        if getattr(r, "duration_s", 0) <= 0:
            problems.append(f"run {rid}: non-positive duration_s ({getattr(r, 'duration_s', None)})")
        if getattr(r, "tokens_in", 0) < 0 or getattr(r, "tokens_out", 0) < 0:
            problems.append(f"run {rid}: negative token count")
    return problems


__all__ = ["looks_like_digest", "validate_submission"]
