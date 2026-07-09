"""`agora bench --no-run` — generates a valid campaign offline (L1-B)."""

from __future__ import annotations

from typer.testing import CliRunner

from agora.cli import app

runner = CliRunner()


def test_bench_no_run_generates_a_schema_valid_campaign(tmp_path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["bench", "gemma-e4b", "--no-run", "--output-dir", str(out)]
    )
    assert result.exit_code == 0, result.stdout
    assert "standard-v1" in result.stdout

    campaign_path = out / "campaign.yaml"
    assert campaign_path.exists()

    import yaml

    from scripts.run_campaign import Campaign, expand_plan

    model = Campaign.model_validate(yaml.safe_load(campaign_path.read_text(encoding="utf-8")))
    assert len(expand_plan(model)) == 6  # 2 arms x 3 repeats, harness-merged
