"""Expand an implicit campaign spec into the explicit, run-tuple form.

The committed ``campaigns/axis-1-tool-call-fidelity.yaml`` is the OUTPUT of this
generator, not a hand-maintained file — re-run it after a profile rename
instead of hand-editing 36 lines.

Implicit spec = ``probes × models × arms × repeats``. The generator emits one
explicit run per combination with a generated ``rNNN`` id, ordered to
**minimize model swaps**: models are the outermost loop, so every run for a
given model is consecutive (the model loads at most once); within a model, all
``repeats`` of one ``(probe, arm)`` are consecutive, then the arm flips, then
the probe changes.

Usage:

    python scripts/expand_campaign.py            # writes the axis-1 campaign
    python scripts/expand_campaign.py --stdout   # print instead of writing
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

# ---- axis-1 implicit spec (the single source the committed YAML derives from) ----

AXIS1_NAME = "axis-1-tool-call-fidelity"
AXIS1_DESCRIPTION = (
    "Axis-1 tool-call fidelity sweep: 1 probe x 6 models x 2 arms x 3 repeats. "
    "Ordered to minimize model swaps. qwen-coder-32b is omitted (VRAM gate). "
    "v1: arm.scaffolding/strictness are recorded but not yet behavioural, so "
    "lean and rich produce identical runs — the sweep is repeatability data."
)
AXIS1_PROBES = ["flows/tool-call-fidelity.plan.yaml"]
AXIS1_MODELS = [
    "qwen-coder-7b",
    "qwen-coder-14b",
    "qwen-instruct-7b",
    "gemma-e4b",
    "mistral-nemo-12b",
    "qwen3-30b",
]
AXIS1_ARMS = [
    {"scaffolding": "lean", "strictness": "strict"},
    {"scaffolding": "rich", "strictness": "strict"},
]
AXIS1_REPEATS = 3
AXIS1_DEFAULTS = {
    "params": {"temperature": 0.0, "seed": 42, "num_ctx": 8192, "max_tokens": 2048},
    "output_dir": "runs_out/axis-1-tool-call-fidelity",
    "resume": True,
}


def expand_runs(
    probes: list[str],
    models: list[str],
    arms: list[dict[str, str]],
    repeats: int,
) -> list[dict[str, Any]]:
    """Cross-product the four axes into explicit run tuples, minimized-swap order.

    Order: models (outer) → probes → arms → repeats (inner). Ids are ``rNNN``
    assigned in emission order.
    """
    runs: list[dict[str, Any]] = []
    n = 0
    for model in models:
        for probe in probes:
            for arm in arms:
                for repeat in range(1, repeats + 1):
                    n += 1
                    runs.append(
                        {
                            "id": f"r{n:03d}",
                            "probe": probe,
                            "profile": model,
                            "arm": dict(arm),
                            "repeat": repeat,
                        }
                    )
    return runs


def build_campaign(
    *,
    name: str,
    description: str,
    defaults: dict[str, Any],
    probes: list[str],
    models: list[str],
    arms: list[dict[str, str]],
    repeats: int,
) -> dict[str, Any]:
    """Assemble the full explicit campaign dict (ready to dump to YAML)."""
    return {
        "schema_version": 1,
        "name": name,
        "description": description,
        "defaults": defaults,
        "runs": expand_runs(probes, models, arms, repeats),
    }


def axis1_campaign() -> dict[str, Any]:
    """The committed axis-1 campaign, expanded from the embedded implicit spec."""
    return build_campaign(
        name=AXIS1_NAME,
        description=AXIS1_DESCRIPTION,
        defaults=AXIS1_DEFAULTS,
        probes=AXIS1_PROBES,
        models=AXIS1_MODELS,
        arms=AXIS1_ARMS,
        repeats=AXIS1_REPEATS,
    )


def dump_campaign_yaml(campaign: dict[str, Any]) -> str:
    """Serialize a campaign dict to YAML with stable key order."""
    return yaml.safe_dump(campaign, sort_keys=False, width=100)


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand the axis-1 campaign spec.")
    parser.add_argument(
        "--out",
        default="campaigns/axis-1-tool-call-fidelity.yaml",
        help="Output path (default campaigns/axis-1-tool-call-fidelity.yaml).",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args()

    text = dump_campaign_yaml(axis1_campaign())
    if args.stdout:
        print(text)
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    n = len(axis1_campaign()["runs"])
    print(f"[*] wrote {out} ({n} runs)")


if __name__ == "__main__":
    main()
