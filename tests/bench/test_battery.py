"""Battery loading + campaign generation (L1-B).

The load-bearing assertion: the generated campaign dict validates against the
REAL campaign schema (scripts.run_campaign.Campaign) — the battery is a generator
of a campaign the existing harness runs unchanged, so the two cannot drift.
"""

from __future__ import annotations

from agora.bench.battery import Battery, battery_to_campaign, load_battery


def test_ships_standard_v1() -> None:
    bat = load_battery("benchmarks/standard-v1.yaml")
    assert bat.battery_version == "standard-v1"
    assert bat.probe == "flows/tool-call-fidelity.plan.yaml"
    assert bat.repeats == 3
    ids = {a.id for a in bat.arms}
    assert ids == {"production", "raw_control"}
    prod = next(a for a in bat.arms if a.id == "production")
    assert prod.harness == {"tool_errors": "corrective", "nudge_budget": 1, "review_budget": 0}


def test_battery_version_is_name_dash_version() -> None:
    bat = Battery(name="standard", version="v2", probe="flows/x.yaml", arms=[])
    assert bat.battery_version == "standard-v2"


def _battery() -> Battery:
    return load_battery("benchmarks/standard-v1.yaml")


def test_campaign_has_arm_times_repeat_runs_with_rNNN_ids() -> None:
    camp = battery_to_campaign(_battery(), "gemma-e4b", "runs_out/bench/x")
    assert len(camp["runs"]) == 2 * 3  # two arms, three repeats
    assert [r["id"] for r in camp["runs"]] == [f"r{n:03d}" for n in range(1, 7)]
    # Each run carries its arm's harness override + the fixed profile/probe.
    assert all(r["profile"] == "gemma-e4b" for r in camp["runs"])
    assert all(r["probe"] == "flows/tool-call-fidelity.plan.yaml" for r in camp["runs"])
    harnesses = {tuple(sorted(r["harness"].items())) for r in camp["runs"]}
    assert len(harnesses) == 2  # production vs raw_control


def test_generated_campaign_validates_against_real_schema() -> None:
    # Proves the battery cannot drift from the campaign harness it feeds.
    from scripts.run_campaign import Campaign, expand_plan

    camp = battery_to_campaign(_battery(), "gemma-e4b", "runs_out/bench/x")
    model = Campaign.model_validate(camp)
    plan = expand_plan(model)  # the harness's own expansion must accept it
    assert len(plan) == 6
    # The production arm's harness override survives the merge.
    prod_runs = [p for p in plan if p["harness"]["tool_errors"] == "corrective"]
    assert len(prod_runs) == 3
    assert all(p["harness"]["nudge_budget"] == 1 for p in prod_runs)
