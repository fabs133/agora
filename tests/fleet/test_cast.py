"""Cast loader + validator (roles-and-casting Stage 1).

Golden test runs against the REAL casts/p40-24gb.yaml + profiles.yaml so a
drift in either file (a deleted profile, an over-budget residency edit) trips
here. Rule tests use synthetic casts built in-memory.
"""

from __future__ import annotations

import pytest

from agora.core.errors import AgoraError
from agora.fleet.cast import (
    Cast,
    CastBinding,
    CastHardware,
    load_cast,
    resolve_cast,
    validate_cast,
)
from agora.fleet.profiles import load_profiles

# Resident-model sizes for the real cast (GB). Injected so the golden test is
# daemon-independent; the live CLI reads these from Ollama /api/show.
_REAL_SIZES = {
    "ollama/gemma4:e4b": 9.6,
    "ollama/qwen2.5:7b-instruct": 4.7,
    "ollama/nomic-embed-text:latest": 0.3,
}


def _profiles():
    return load_profiles("profiles.yaml")


def _cast(**bindings) -> Cast:
    return Cast(
        name="t",
        hardware=CastHardware(gpu="T", vram_budget_gb=24),
        bindings={r: CastBinding(**b) for r, b in bindings.items()},
    )


# --------------------------------------------------------------- golden (real files)


def test_real_cast_loads_and_validates_clean() -> None:
    cast = load_cast("casts/p40-24gb.yaml")
    assert cast.name == "p40-24gb"
    assert set(cast.bindings) == {
        "implementer", "tester", "verifier", "planner", "classifier", "retriever"
    }
    # World B: the conditional implementer_secondary seat was deleted.
    assert "implementer_secondary" not in cast.bindings
    assert validate_cast(cast, _profiles(), sizes_gb=_REAL_SIZES) == []


def test_real_cast_role_table_resolves() -> None:
    cast = load_cast("casts/p40-24gb.yaml")
    table = {rb.role: rb for rb in resolve_cast(cast, _profiles())}
    assert table["implementer"].model == "ollama/gemma4:e4b"
    assert table["implementer"].resident is True
    assert table["verifier"].model == "ollama/qwen2.5:7b-instruct"
    # tester shares the implementer's gemma-e4b (turf-separated seat, one load).
    assert table["tester"].model == "ollama/gemma4:e4b"
    assert table["tester"].resident is True
    assert table["planner"].is_human is True
    assert table["planner"].profile is None
    # classifier is load-on-demand, not resident.
    assert table["classifier"].resident is False


def test_real_cast_residency_skipped_without_sizes() -> None:
    """Rule 2 is honestly reported as un-checked, not silently passed."""
    cast = load_cast("casts/p40-24gb.yaml")
    errs = validate_cast(cast, _profiles())
    assert errs == ["residency check skipped: no model sizes supplied "
                    "(pass sizes_gb from the manifest store)"]


# --------------------------------------------------------------- rule 1: resolution


def test_rule1_unknown_profile_fails() -> None:
    cast = _cast(implementer={"profile": "no-such-profile", "evidence": {"gate": "x"}})
    errs = validate_cast(cast, _profiles(), sizes_gb={})
    assert any("not in profiles.yaml" in e for e in errs)


# --------------------------------------------------------------- rule 2: residency


def test_rule2_shared_resident_model_counts_once() -> None:
    """Two roles bound to the same resident model share one load (dedup by id)."""
    cast = _cast(
        implementer={"profile": "gemma-e4b", "resident": True, "evidence": {"g": 1}},
        tester={"profile": "gemma-e4b", "resident": True, "evidence": {"g": 1}},
    )
    # gemma counted ONCE = 9.6, not 19.2 → within a 12 GB budget.
    cast = Cast(name="t", hardware=CastHardware(gpu="T", vram_budget_gb=12), bindings=cast.bindings)
    assert validate_cast(cast, _profiles(), sizes_gb={"ollama/gemma4:e4b": 9.6}) == []


def test_rule2_over_budget_fails() -> None:
    cast = _cast(
        a={"profile": "gemma-e4b", "resident": True, "evidence": {"g": 1}},
        b={"profile": "qwen3-30b", "resident": True, "evidence": {"g": 1}},
    )
    sizes = {"ollama/gemma4:e4b": 9.6, "ollama/qwen3:30b": 18.6}  # 28.2 > 24
    errs = validate_cast(cast, _profiles(), sizes_gb=sizes)
    assert any("exceeds vram_budget" in e for e in errs)


def test_rule2_missing_size_flagged() -> None:
    cast = _cast(a={"profile": "gemma-e4b", "resident": True, "evidence": {"g": 1}})
    errs = validate_cast(cast, _profiles(), sizes_gb={})  # no size for the model
    assert any("no size for resident model" in e for e in errs)


# ------------------------------------------------------- rule 3 + 4: evidence / human


def test_rule3_missing_evidence_and_waiver_fails() -> None:
    cast = _cast(implementer={"profile": "gemma-e4b"})  # no evidence, no waiver
    errs = validate_cast(cast, _profiles(), sizes_gb={})
    assert any("neither evidence nor waiver" in e for e in errs)


def test_waiver_satisfies_rule3() -> None:
    cast = _cast(verifier={"profile": "qwen-instruct-7b", "waiver": "unmeasured; axis-2"})
    assert validate_cast(cast, _profiles(), sizes_gb={}) == []


def test_rule4_human_binding_always_valid() -> None:
    cast = _cast(planner={"binding": "human"})
    assert validate_cast(cast, _profiles(), sizes_gb={}) == []


def test_human_binding_needs_no_profile_or_evidence() -> None:
    # A human seat with neither profile nor evidence nor waiver is still valid.
    cast = _cast(planner={"binding": "human", "resident": False})
    assert validate_cast(cast, _profiles(), sizes_gb={}) == []


def test_binding_with_neither_profile_nor_human_fails() -> None:
    cast = _cast(orphan={"evidence": {"g": 1}})
    errs = validate_cast(cast, _profiles(), sizes_gb={})
    assert any("needs a profile or binding:human" in e for e in errs)


def test_unknown_binding_kind_fails() -> None:
    cast = _cast(x={"binding": "robot", "evidence": {"g": 1}})
    errs = validate_cast(cast, _profiles(), sizes_gb={})
    assert any("unknown binding kind" in e for e in errs)


# --------------------------------------------------------------- load / resolve guards


def test_load_cast_missing_file_raises() -> None:
    with pytest.raises(AgoraError, match="not found"):
        load_cast("casts/does-not-exist.yaml")


def test_resolve_refuses_invalid_cast() -> None:
    cast = _cast(implementer={"profile": "no-such-profile", "evidence": {"g": 1}})
    with pytest.raises(AgoraError, match="cannot load invalid cast"):
        resolve_cast(cast, _profiles())
