"""Unit tests for the campaign generator (expansion order + axis-1 output)."""

from __future__ import annotations

from scripts.expand_campaign import axis1_campaign, expand_runs


def test_expand_runs_minimized_swap_order() -> None:
    runs = expand_runs(
        probes=["p"],
        models=["m1", "m2"],
        arms=[{"scaffolding": "lean", "strictness": "strict"},
              {"scaffolding": "rich", "strictness": "strict"}],
        repeats=2,
    )
    assert len(runs) == 8  # 1 probe × 2 models × 2 arms × 2 repeats
    assert [r["id"] for r in runs] == [f"r{i:03d}" for i in range(1, 9)]
    # Model is the outermost axis: all of m1 before any m2.
    profiles = [r["profile"] for r in runs]
    assert profiles == ["m1"] * 4 + ["m2"] * 4
    # Within a model: both repeats of (arm) consecutive, then arm flips.
    m1 = runs[:4]
    assert [r["arm"]["scaffolding"] for r in m1] == ["lean", "lean", "rich", "rich"]
    assert [r["repeat"] for r in m1] == [1, 2, 1, 2]


def test_axis1_campaign_has_36_runs_in_committed_order() -> None:
    camp = axis1_campaign()
    runs = camp["runs"]
    assert len(runs) == 36
    assert [r["id"] for r in runs] == [f"r{i:03d}" for i in range(1, 37)]
    # First model fully consumed (2 arms × 3 repeats = 6) before the second.
    assert {r["profile"] for r in runs[:6]} == {"qwen-coder-7b"}
    assert {r["profile"] for r in runs[6:12]} == {"qwen-coder-14b"}
    # qwen-coder-32b is deliberately omitted.
    assert "qwen-coder-32b" not in {r["profile"] for r in runs}
    # Defaults pinned.
    assert camp["defaults"]["params"] == {
        "temperature": 0.0, "seed": 42, "num_ctx": 8192, "max_tokens": 2048
    }
    assert camp["defaults"]["output_dir"] == "runs_out/axis-1-tool-call-fidelity"
    assert camp["defaults"]["resume"] is True
    # Short review timeout so the REVIEW phase doesn't idle 300s/run headless.
    assert camp["defaults"]["review_timeout_seconds"] == 5


def test_model_loads_at_most_twice_per_model() -> None:
    """Consecutive same-model runs mean each model loads once across its 6 runs."""
    runs = axis1_campaign()["runs"]
    # Count model "boundaries" — a profile change between consecutive runs.
    switches = sum(
        1 for a, b in zip(runs, runs[1:], strict=False)
        if a["profile"] != b["profile"]
    )
    # 6 models in a row ⇒ exactly 5 switches (one load per model).
    assert switches == 5
