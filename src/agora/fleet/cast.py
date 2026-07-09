"""Cast loader + validator (roles-and-casting Stage 1).

A CAST binds each ROLE to a model profile for one hardware envelope, with
evidence citations. This module loads a ``casts/<envelope>.yaml`` file into a
validated :class:`Cast` and checks it against the four rules from
``docs/design/roles-and-casting.md``:

1. Every ``profile`` reference resolves in ``profiles.yaml``.
2. Sum of resident model sizes ≤ ``vram_budget_gb`` (sizes from the local
   manifest store — Ollama ``/api/show`` — not hand-entered).
3. Every binding either cites ``evidence`` or carries a ``waiver`` …
4. … UNLESS it is ``binding: human``, which is always valid and needs neither.

Loading (:func:`resolve_cast`) produces the role→profile table the orchestrator
would consume; the ``resident``/``keep_alive`` fields are advisory to the
eviction protocol. Deliberately small: schema + four rules + loud failures.
Model-size lookup is injected so the validator stays pure and testable — the
CLI supplies the live Ollama probe, tests supply a static map.

Finding: rule 3's ``evidence`` requirement is where **F12** lands — a binding's
evidence key is per-(model × TOOL SURFACE), not per-model alone. A model measured
reliable on one tool family has not been shown reliable casting onto a seat that
uses a different, unmeasured one (the run-1.3 add_function wall). So "cite
evidence" means cite a row for the seat's actual tools, not a generic 9/9.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from agora.core.errors import AgoraError
from agora.fleet.profiles import ModelProfile, ProfileSet

#: The sentinel value for a role deliberately bound to a human, not a model.
HUMAN_BINDING = "human"


class CastBinding(BaseModel):
    """One role→profile binding inside a cast.

    Exactly one of ``profile`` or ``binding: human`` should be set (checked in
    :func:`validate_cast`, not here, so the error is reported not raised).
    """

    model_config = {"extra": "forbid"}

    profile: str | None = None
    binding: str | None = None  # only recognised value: "human"
    resident: bool = False
    keep_alive: str = "30m"
    evidence: dict[str, Any] | None = None
    waiver: str | None = None


class CastHardware(BaseModel):
    """The hardware envelope a cast targets."""

    model_config = {"extra": "forbid"}

    gpu: str = ""
    gpu_uuid: str = ""
    vram_budget_gb: float


class Cast(BaseModel):
    """A full cast: hardware envelope + role bindings."""

    model_config = {"extra": "forbid"}

    schema_version: int = 1
    name: str
    hardware: CastHardware
    bindings: dict[str, CastBinding]


class ResolvedBinding(BaseModel):
    """One entry of the role table produced by :func:`resolve_cast`.

    ``profile`` is None for a human-bound role. ``model`` is the concrete model
    id (e.g. ``ollama/gemma4:e4b``) for a profile binding, else "".
    """

    model_config = {"extra": "forbid"}

    role: str
    is_human: bool
    profile: ModelProfile | None = None
    model: str = ""
    resident: bool = False
    keep_alive: str = "30m"


def load_cast(path: str | Path) -> Cast:
    """Load and schema-validate a cast YAML file.

    YAML parse errors and pydantic validation failures both surface as
    :class:`AgoraError` with the offending path attached — mirrors
    :func:`agora.fleet.profiles.load_profiles`.
    """
    p = Path(path)
    if not p.is_file():
        raise AgoraError(f"cast file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AgoraError(f"cast at {p} could not be parsed as YAML: {exc}") from exc
    try:
        return Cast.model_validate(raw)
    except ValidationError as exc:
        raise AgoraError(f"cast at {p} failed schema validation:\n{exc}") from exc


def _is_matrix_citation(evidence: Any) -> bool:
    """A binding's evidence is a MATRIX-ROW citation (vs free-text) when it names a
    ``model_digest`` — the row identity. Free-text ``{campaign, gate}`` is not."""
    return isinstance(evidence, dict) and "model_digest" in evidence


def _verify_matrix_citation(role: str, binding: CastBinding, matrix: Any, roles: Any, errors: list[str]) -> None:
    """A cited model must be ELIGIBLE for the role at the role's harness key."""
    from agora.bench.eligibility import eligible_digests

    role_obj = getattr(roles, "roles", {}).get(role)
    if role_obj is None:
        errors.append(f"{role}: cites matrix evidence but is absent from roles.yaml")
        return
    if role_obj.measured is None:
        errors.append(f"{role}: role is unmeasured/task_specific — cite a waiver, not matrix evidence")
        return
    digest = binding.evidence.get("model_digest") if binding.evidence else None
    probe_version = binding.evidence.get("probe_version") if binding.evidence else None
    if digest not in eligible_digests(matrix, role_obj, probe_version=probe_version):
        errors.append(
            f"{role}: cited model {digest!r} has no passing measurement for role "
            f"{role!r} at its harness key — not eligible"
        )


def validate_cast(
    cast: Cast,
    profiles: ProfileSet,
    *,
    sizes_gb: Mapping[str, float] | None = None,
    matrix: Any = None,
    roles: Any = None,
) -> list[str]:
    """Check a cast against the four casting rules; return a list of problems.

    An empty list means the cast is valid. ``sizes_gb`` maps a concrete model
    id (``profile.model``) → its on-disk size in GB; supply it (from the local
    manifest store) to enable the residency-budget rule. When it is None, the
    residency sum is not checkable and is reported as a single skip note rather
    than silently passing.

    Rule 3 is dual-accept (L1-C): free-text ``evidence`` or a ``waiver`` satisfies
    it as before. A MATRIX-ROW citation (evidence naming a ``model_digest``) is
    additionally VERIFIED when both ``matrix`` (the capability matrix DataFrame)
    and ``roles`` (a :class:`~agora.fleet.roles.RoleSet`) are supplied — the cited
    model must be eligible for the role. Absent those, a citation is accepted with
    a single skip note (never silently passed as verified).
    """
    errors: list[str] = []
    resident: list[tuple[str, str]] = []  # (role, model)
    citation_skips: list[str] = []

    for role, b in cast.bindings.items():
        is_human = b.binding == HUMAN_BINDING
        if b.binding is not None and not is_human:
            errors.append(f"{role}: unknown binding kind {b.binding!r} (only {HUMAN_BINDING!r})")
        # Exactly one of profile / human.
        if is_human and b.profile:
            errors.append(f"{role}: has both binding:human and a profile — pick one")
        if not is_human and not b.profile:
            errors.append(f"{role}: needs a profile or binding:{HUMAN_BINDING}")

        # Rule 4: human bindings are always valid; skip evidence + resolution.
        if is_human:
            continue

        # Rule 1: profile resolves.
        known = b.profile in profiles.profiles if b.profile else False
        if b.profile and not known:
            errors.append(
                f"{role}: profile {b.profile!r} not in profiles.yaml "
                f"(available: {sorted(profiles.profiles)})"
            )

        # Rule 3: evidence OR waiver (free-text still satisfies — dual-accept). A
        # matrix-row citation is verified when a matrix + roles are supplied.
        if not b.evidence and not b.waiver:
            errors.append(f"{role}: binding cites neither evidence nor waiver")
        elif _is_matrix_citation(b.evidence):
            if matrix is None or roles is None:
                citation_skips.append(role)
            else:
                _verify_matrix_citation(role, b, matrix, roles, errors)

        if b.resident and b.profile and known:
            resident.append((role, profiles.profiles[b.profile].model))

    # Rule 2: resident sizes sum ≤ budget. Sum DISTINCT model ids — two roles
    # bound to the same resident model (implementer + tester → gemma-e4b) share
    # ONE load, so the model's size counts once.
    if resident:
        if sizes_gb is None:
            errors.append(
                "residency check skipped: no model sizes supplied "
                "(pass sizes_gb from the manifest store)"
            )
        else:
            total = 0.0
            counted: set[str] = set()
            for role, model in resident:
                sz = sizes_gb.get(model)
                if sz is None:
                    errors.append(f"{role}: no size for resident model {model!r} in manifest store")
                elif model not in counted:
                    counted.add(model)
                    total += sz
            if total > cast.hardware.vram_budget_gb:
                errors.append(
                    f"resident total {total:.1f} GB exceeds vram_budget "
                    f"{cast.hardware.vram_budget_gb} GB"
                )

    if citation_skips:
        errors.append(
            f"matrix-citation check skipped for {sorted(set(citation_skips))}: "
            f"no matrix/roles supplied (pass matrix= and roles= to verify)"
        )

    return errors


def resolve_cast(cast: Cast, profiles: ProfileSet) -> list[ResolvedBinding]:
    """Build the role table a loader would hand the orchestrator.

    Resolves each profile binding to its :class:`ModelProfile`; leaves human
    bindings profile-less. Raises :class:`AgoraError` if the cast does not
    validate structurally (unknown profile / malformed binding) — load refuses
    an invalid cast. Residency-budget failures do NOT block load (residency is
    advisory to the eviction protocol), so this ignores ``sizes_gb``.
    """
    structural = [
        e for e in validate_cast(cast, profiles)
        if "residency check skipped" not in e
    ]
    if structural:
        joined = "\n  - ".join(structural)
        raise AgoraError(f"cannot load invalid cast {cast.name!r}:\n  - {joined}")

    table: list[ResolvedBinding] = []
    for role, b in cast.bindings.items():
        if b.binding == HUMAN_BINDING:
            table.append(ResolvedBinding(role=role, is_human=True, keep_alive=b.keep_alive))
            continue
        prof = profiles.profiles[b.profile]  # resolution guaranteed by validate above
        table.append(
            ResolvedBinding(
                role=role,
                is_human=False,
                profile=prof,
                model=prof.model,
                resident=b.resident,
                keep_alive=b.keep_alive,
            )
        )
    return table


async def ollama_sizes_gb(
    cast: Cast, profiles: ProfileSet, base_url: str
) -> dict[str, float]:
    """Best-effort: query the local Ollama manifest store for resident sizes.

    Returns ``{model_id: size_gb}`` for every RESIDENT profile binding whose
    profile resolves. Used by the CLI to feed :func:`validate_cast`'s residency
    rule; kept out of the pure validator so tests need no daemon.
    """
    from agora.fleet.vram import get_model_size_mib

    out: dict[str, float] = {}
    for b in cast.bindings.values():
        if not (b.resident and b.profile and b.profile in profiles.profiles):
            continue
        model = profiles.profiles[b.profile].model
        if model in out:
            continue
        mib = await get_model_size_mib(model, base_url)
        out[model] = round(mib / 1024, 2)
    return out


__all__ = [
    "Cast",
    "CastBinding",
    "CastHardware",
    "HUMAN_BINDING",
    "ResolvedBinding",
    "load_cast",
    "ollama_sizes_gb",
    "resolve_cast",
    "validate_cast",
]
