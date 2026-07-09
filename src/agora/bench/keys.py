"""The re-derivable capability-matrix key (capability-program L1-A).

A capability-vector row is comparable ONLY within one key. Cross-key pooling is
impossible by construction: every matrix row carries the full key, and the query
layer refuses to mix keys without an explicit opt-in (see :mod:`agora.bench.matrix`).

Key = ``(model_digest, battery_version, probe_version, harness_hash,
daemon_version)``. Hardware / quantization / date are metadata, not key.

``harness_hash`` (owner decision, 2026-07-09): SHA-256 over sorted-key JSON of
the BEHAVIOUR-affecting harness fields ONLY — endpoints, paths and credentials
are never in the hash. The field set is pinned in :data:`HARNESS_HASH_FIELDS`;
a field absent from a run's recorded harness hashes at its documented default,
so a partial harness dict (older runs recorded only three keys) still produces a
stable, meaningful hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: The five columns that make a capability-vector row comparable. Every matrix
#: row carries all five; a query that spans more than one distinct tuple must
#: opt in explicitly (never silent pooling).
KEY_FIELDS: tuple[str, ...] = (
    "model_digest",
    "battery_version",
    "probe_version",
    "harness_hash",
    "daemon_version",
)

#: Behaviour-affecting harness fields, in canonical order, each with the default
#: applied when a run's recorded harness omits it. Pinned: changing this set (or
#: a default) changes every hash and so is a new battery/harness generation, not
#: an in-place edit. Endpoints/paths/credentials are deliberately absent.
HARNESS_HASH_FIELDS: tuple[tuple[str, Any], ...] = (
    ("tool_errors", "raw"),
    ("nudge_budget", 0),
    ("review_budget", 0),
    ("salvage_budget", 0),
    ("routed_retry_budget", 2),
    ("max_task_retries", 2),
)

#: Length of the harness-hash prefix carried in the key (full SHA-256 is 64 hex).
HARNESS_HASH_LEN = 12


def harness_hash(harness: Mapping[str, Any] | None) -> str:
    """Stable short hash of the behaviour-affecting harness config.

    Only :data:`HARNESS_HASH_FIELDS` participate; each missing field falls back
    to its documented default, so the hash depends on *behaviour*, not on which
    keys a particular runner happened to record. Values are coerced to their
    default's type where unambiguous (e.g. ``"1"`` -> ``1`` for an int field) so
    an env-string and a typed value hash identically.
    """
    src: Mapping[str, Any] = harness or {}
    canonical: dict[str, Any] = {}
    for field, default in HARNESS_HASH_FIELDS:
        value = src.get(field, default)
        canonical[field] = _coerce_like(value, default)
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HARNESS_HASH_LEN]


def _coerce_like(value: Any, default: Any) -> Any:
    """Coerce ``value`` to ``default``'s type when that is unambiguous (int/str),
    so ``"1"`` and ``1`` — or an env-string and a typed budget — hash the same.
    Falls back to the raw value if coercion fails (the hash still differs loudly
    rather than silently swallowing a bad value)."""
    if isinstance(default, bool):
        # bool before int: avoid int("true") paths; accept common truthy strings.
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if isinstance(default, str):
        return str(value)
    return value


def harness_hash_inputs(harness: Mapping[str, Any] | None) -> dict[str, Any]:
    """The exact canonical dict :func:`harness_hash` digests — for diagnostics
    and for a human-readable column beside the opaque hash."""
    src: Mapping[str, Any] = harness or {}
    return {f: _coerce_like(src.get(f, d), d) for f, d in HARNESS_HASH_FIELDS}
