"""Rule-3 dual-accept: free-text evidence still valid; matrix-row citations verified (L1-C)."""

from __future__ import annotations

import pandas as pd

from agora.bench.keys import harness_hash
from agora.bench.matrix import MATRIX_COLUMNS
from agora.fleet.cast import Cast, CastBinding, CastHardware, validate_cast
from agora.fleet.profiles import ModelProfile, ProfileSet
from agora.fleet.roles import load_roles

_ROLES = load_roles("roles.yaml")
_HH = harness_hash(_ROLES.role("implementer").harness)


def _profiles() -> ProfileSet:
    return ProfileSet(profiles={"gemma-e4b": ModelProfile(model="ollama/gemma4:e4b")})


def _cast(evidence=None, waiver=None) -> Cast:
    return Cast(
        name="t",
        hardware=CastHardware(vram_budget_gb=24),
        bindings={"implementer": CastBinding(profile="gemma-e4b", evidence=evidence, waiver=waiver)},
    )


def _matrix(digest, pass_rate):
    def row(sub_target, raw):
        r = dict.fromkeys(MATRIX_COLUMNS)
        r.update(
            model_digest=digest, battery_version="standard-v1", probe_version=7,
            harness_hash=_HH, daemon_version="0.1.0", model="gemma-e4b", strategy=None,
            axis="tool_call_fidelity", sub_target=sub_target, raw_value=raw,
            date="2026-07-01", source="local",
        )
        return r

    return pd.DataFrame(
        [row("pass_rate", pass_rate), row("trajectory_reproducibility_rate", 1.0)]
    ).reindex(columns=list(MATRIX_COLUMNS))


def test_free_text_evidence_still_valid_without_a_matrix() -> None:
    # Dual-accept: the historical free-text citation is unchanged.
    assert validate_cast(_cast(evidence={"campaign": "axis-1-v8", "gate": "9/9"}), _profiles()) == []


def test_matrix_citation_accepted_with_skip_note_when_no_matrix() -> None:
    errs = validate_cast(_cast(evidence={"model_digest": "sha:pass"}), _profiles())
    assert any("matrix-citation check skipped" in e for e in errs)


def test_matrix_citation_verified_eligible() -> None:
    cast = _cast(evidence={"model_digest": "sha:pass", "probe_version": 7})
    errs = validate_cast(cast, _profiles(), matrix=_matrix("sha:pass", 1.0), roles=_ROLES)
    assert errs == []


def test_matrix_citation_rejected_when_not_eligible() -> None:
    cast = _cast(evidence={"model_digest": "sha:low", "probe_version": 7})
    errs = validate_cast(cast, _profiles(), matrix=_matrix("sha:low", 0.5), roles=_ROLES)
    assert any("not eligible" in e for e in errs)


def test_matrix_citation_rejected_when_row_absent() -> None:
    cast = _cast(evidence={"model_digest": "sha:missing", "probe_version": 7})
    errs = validate_cast(cast, _profiles(), matrix=_matrix("sha:other", 1.0), roles=_ROLES)
    assert any("not eligible" in e for e in errs)
