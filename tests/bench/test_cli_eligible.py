"""`agora cast eligible` — matrix query behind the CLI (L1-C)."""

from __future__ import annotations

import pandas as pd
from typer.testing import CliRunner

from agora.bench.keys import harness_hash
from agora.bench.matrix import MATRIX_COLUMNS
from agora.cli import app
from agora.fleet.roles import load_roles

runner = CliRunner()
_HH = harness_hash(load_roles("roles.yaml").role("implementer").harness)


def _write_matrix(path, digest, pass_rate):
    def row(sub_target, raw):
        r = dict.fromkeys(MATRIX_COLUMNS)
        r.update(
            model_digest=digest, battery_version="standard-v1", probe_version=7,
            harness_hash=_HH, daemon_version="0.1.0", model="gemma-e4b", strategy="",
            axis="tool_call_fidelity", sub_target=sub_target, raw_value=raw,
            date="2026-07-01", source="local",
        )
        return r

    df = pd.DataFrame(
        [row("pass_rate", pass_rate), row("trajectory_reproducibility_rate", 1.0)]
    ).reindex(columns=list(MATRIX_COLUMNS))
    df.to_csv(path, index=False)


def test_eligible_lists_a_passing_model(tmp_path) -> None:
    csv = tmp_path / "m.csv"
    _write_matrix(csv, "sha256:pass", 1.0)
    result = runner.invoke(app, ["cast", "eligible", "implementer", "--matrix", str(csv)])
    assert result.exit_code == 0, result.stdout
    assert "Eligible for 'implementer'" in result.stdout
    assert "gemma-e4b" in result.stdout


def test_eligible_reports_none_with_failure_reasons(tmp_path) -> None:
    csv = tmp_path / "m.csv"
    _write_matrix(csv, "sha256:low", 0.5)
    result = runner.invoke(app, ["cast", "eligible", "implementer", "--matrix", str(csv)])
    assert result.exit_code == 0
    assert "No models eligible" in result.stdout
    assert "pass_rate" in result.stdout


def test_eligible_unmeasured_role_says_so(tmp_path) -> None:
    csv = tmp_path / "m.csv"
    _write_matrix(csv, "sha256:pass", 1.0)
    result = runner.invoke(app, ["cast", "eligible", "planner", "--matrix", str(csv)])
    assert result.exit_code == 0
    assert "not matrix-measured" in result.stdout
