"""Battery format + campaign generation (capability-program L1-B).

A *battery* is a named, versioned bundle of probe runs — the fixed measurement
one model is put through so its capability vector is comparable to every other
model's. ``battery_version`` (``<name>-<version>``, e.g. ``standard-v1``) is part
of the matrix key: a battery versions like a probe, and v1 is never mutated in
place (a change is v2).

:func:`battery_to_campaign` expands a battery + a chosen profile into a plain
campaign dict that the existing campaign harness (``scripts/run_campaign.py``)
runs unchanged — the battery is a *generator* of a campaign, not a new runner.
Each battery arm becomes a per-run harness override; the campaign ``arm`` field
(scaffolding/strictness) stays fixed. Emitting a dict (not a Campaign object)
keeps this module free of any ``scripts/`` import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class BatteryArm(BaseModel):
    """One measurement condition. ``harness`` is a per-run override of the
    campaign harness (``tool_errors`` / ``nudge_budget`` / ``review_budget`` — the
    fields the campaign :class:`~run_campaign.Harness` accepts); it is what makes
    two arms of one battery land in DIFFERENT capability-matrix keys."""

    model_config = {"extra": "forbid"}

    id: str
    harness: dict[str, Any] = Field(default_factory=dict)
    scaffolding: Literal["lean", "rich"] = "rich"
    strictness: Literal["strict", "permissive"] = "strict"


class Battery(BaseModel):
    """A named, versioned probe bundle. ``extra='forbid'`` so a typo'd key fails
    at load, not silently at run."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = 1
    name: str
    version: str
    probe: str
    repeats: int = Field(default=3, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)
    review_timeout_seconds: float | None = None
    arms: list[BatteryArm]

    @property
    def battery_version(self) -> str:
        """The key value carried by every row this battery produces."""
        return f"{self.name}-{self.version}"


def load_battery(path: str | Path) -> Battery:
    """Load + validate a battery YAML. Raises ``ValidationError`` on a schema
    mismatch (at LOAD time, before any run)."""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Battery.model_validate(raw)


def battery_to_campaign(battery: Battery, profile: str, output_dir: str | Path) -> dict[str, Any]:
    """Expand ``battery`` for one ``profile`` into a campaign dict (ready to dump
    to YAML and hand to ``scripts/run_campaign.py``).

    Runs are ``arm x repeat`` in declared order, ids ``rNNN`` (the campaign
    convention :func:`agora.observe.analysis.load_run_records` matches subdirs on).
    Each run carries its arm's harness override; the campaign ``arm`` is the arm's
    scaffolding/strictness.
    """
    runs: list[dict[str, Any]] = []
    n = 0
    for arm in battery.arms:
        for repeat in range(1, battery.repeats + 1):
            n += 1
            runs.append(
                {
                    "id": f"r{n:03d}",
                    "probe": battery.probe,
                    "profile": profile,
                    "arm": {"scaffolding": arm.scaffolding, "strictness": arm.strictness},
                    "repeat": repeat,
                    "harness": dict(arm.harness),
                }
            )
    defaults: dict[str, Any] = {
        "params": dict(battery.params),
        "output_dir": str(output_dir),
        "harness": {},  # base; each run's arm harness overrides it
    }
    if battery.review_timeout_seconds is not None:
        defaults["review_timeout_seconds"] = battery.review_timeout_seconds
    return {
        "schema_version": 1,
        "name": f"bench-{battery.battery_version}-{profile}",
        "description": f"agora bench: {profile} through battery {battery.battery_version}",
        "defaults": defaults,
        "runs": runs,
    }
