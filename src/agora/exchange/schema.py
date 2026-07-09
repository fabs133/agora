"""Submission schema (capability-program L2-0).

A submission is: a MANIFEST (the claim — the full key + the derived vector rows),
raw RECORDS (runs.jsonl, gzipped when packaged), and an ATTESTATION (the
environment testimony — GPU, driver, digest, quantization, OS). The manifest's
vector is never trusted as claimed: the validator re-derives it from the records
(:mod:`agora.exchange.validate`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from agora.bench.keys import KEY_FIELDS

#: Bumped when the submission format changes incompatibly.
SUBMISSION_SCHEMA_VERSION = 1

#: Manifest vector-row columns: the full key + the cell + the value + the date.
#: Hardware/source live in the attestation/provenance, not the row.
_VALUE_COLUMNS: tuple[str, ...] = (
    "raw_value",
    "normalized_score",
    "repeats",
    "excluded_repeats",
    "ci_low",
    "ci_high",
)
MANIFEST_ROW_COLUMNS: tuple[str, ...] = (
    *KEY_FIELDS,
    "model",
    "strategy",
    "axis",
    "sub_target",
    *_VALUE_COLUMNS,
    "date",
)


class VectorRow(BaseModel):
    """One derived capability-vector cell carrying its full key."""

    model_config = {"extra": "forbid"}

    model_digest: str
    battery_version: str
    probe_version: int
    harness_hash: str
    daemon_version: str
    model: str
    strategy: str | None = None
    axis: str
    sub_target: str
    raw_value: float | None = None
    normalized_score: float | None = None
    repeats: int
    excluded_repeats: int
    ci_low: float | None = None
    ci_high: float | None = None
    date: str = ""


class Manifest(BaseModel):
    """The submission claim: the model + battery + probe this covers, plus every
    derived vector row (across the battery's harness arms). Re-derived, not trusted."""

    model_config = {"extra": "forbid"}

    submission_schema_version: Literal[1] = SUBMISSION_SCHEMA_VERSION
    model_digest: str
    battery_version: str
    probe_version: int
    rows: list[VectorRow]


class Attestation(BaseModel):
    """Environment testimony. Labeled as such: the exchange does not verify
    hardware it cannot touch. The digest/battery here are cross-checked against
    the manifest and used to re-derive."""

    model_config = {"extra": "forbid"}

    submission_schema_version: Literal[1] = SUBMISSION_SCHEMA_VERSION
    model_digest: str
    battery_version: str
    daemon_version: str
    gpu: str = ""
    driver: str = ""
    quantization: str = ""
    os: str = ""
    contributor: str = ""  # gh-user or free label; sanitized of machine-private strings


def manifest_rows_from_matrix(df: Any) -> list[dict[str, Any]]:
    """Project a derived matrix DataFrame (MATRIX_COLUMNS) to manifest row dicts
    (drops hardware/source, which are attestation/provenance, not the row)."""
    out: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        out.append({c: _clean(r[c]) for c in MANIFEST_ROW_COLUMNS})
    return out


def _clean(value: Any) -> Any:
    """pandas NA / numpy scalar -> plain Python (so YAML/JSON round-trips cleanly)."""
    import math

    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    # numpy scalar -> python
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            return value
    return value


def build_manifest(df: Any) -> Manifest:
    """Assemble a :class:`Manifest` from a single model's derived matrix rows."""
    rows = manifest_rows_from_matrix(df)
    if not rows:
        raise ValueError("build_manifest: no rows to build a manifest from")
    digests = {r["model_digest"] for r in rows}
    if len(digests) != 1:
        raise ValueError(f"a submission covers ONE model; got digests {digests}")
    batteries = {r["battery_version"] for r in rows}
    probes = {r["probe_version"] for r in rows}
    if len(batteries) != 1 or len(probes) != 1:
        raise ValueError(f"a submission covers ONE battery+probe; got {batteries} x {probes}")
    return Manifest(
        model_digest=next(iter(digests)),
        battery_version=next(iter(batteries)),
        probe_version=int(next(iter(probes))),
        rows=[VectorRow.model_validate(r) for r in rows],
    )


__all__ = [
    "MANIFEST_ROW_COLUMNS",
    "SUBMISSION_SCHEMA_VERSION",
    "Attestation",
    "Manifest",
    "VectorRow",
    "build_manifest",
    "manifest_rows_from_matrix",
]
