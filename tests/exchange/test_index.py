"""Index builder: reproduction counting + conflict detection (L2-2 core)."""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from agora.cli import app
from agora.exchange.index import build_index, render_conflicts, write_index
from agora.exchange.schema import Attestation, Manifest, VectorRow

_DIGEST = "sha256:aaaa1111bbbb"


def _sub(dirp, contributor, pass_rate):
    dirp.mkdir(parents=True, exist_ok=True)
    rows = [
        VectorRow(
            model_digest=_DIGEST, battery_version="standard-v1", probe_version=7,
            harness_hash="hhprod12", daemon_version="0.1.0", model="gemma-e4b",
            strategy=None, axis="tool_call_fidelity", sub_target="pass_rate",
            raw_value=pass_rate, repeats=3, excluded_repeats=0, date="2026-07-01",
        )
    ]
    m = Manifest(model_digest=_DIGEST, battery_version="standard-v1", probe_version=7, rows=rows)
    a = Attestation(model_digest=_DIGEST, battery_version="standard-v1",
                    daemon_version="0.1.0", contributor=contributor)
    (dirp / "manifest.yaml").write_text(yaml.safe_dump(m.model_dump()), encoding="utf-8")
    (dirp / "attestation.yaml").write_text(yaml.safe_dump(a.model_dump()), encoding="utf-8")
    return dirp


def test_single_submission_is_one_reproduction(tmp_path) -> None:
    res = build_index([_sub(tmp_path / "s1", "alice", 1.0)])
    assert len(res.matrix) == 1
    row = res.matrix.iloc[0]
    assert row["reproductions"] == 1 and not row["conflicted"]
    assert row["contributors"] == "alice"
    assert res.conflicts == []


def test_agreeing_submissions_raise_reproduction_count(tmp_path) -> None:
    res = build_index([_sub(tmp_path / "s1", "alice", 1.0), _sub(tmp_path / "s2", "bob", 1.0)])
    assert len(res.matrix) == 1  # one cell
    row = res.matrix.iloc[0]
    assert row["reproductions"] == 2 and not row["conflicted"]
    assert row["contributors"] == "alice,bob"
    assert res.conflicts == []


def test_disagreeing_submissions_are_a_conflict_not_an_average(tmp_path) -> None:
    res = build_index([_sub(tmp_path / "s1", "alice", 1.0), _sub(tmp_path / "s2", "bob", 0.66)])
    assert len(res.conflicts) == 1
    clusters = res.conflicts[0]["clusters"]
    assert {c["raw_value"] for c in clusters} == {1.0, 0.66}
    # The matrix keeps the cell flagged, never a silent average of 0.83.
    row = res.matrix.iloc[0]
    assert row["conflicted"]
    assert row["raw_value"] in (1.0, 0.66)


def test_render_conflicts_lists_each_value(tmp_path) -> None:
    res = build_index([_sub(tmp_path / "s1", "alice", 1.0), _sub(tmp_path / "s2", "bob", 0.66)])
    md = render_conflicts(res.conflicts)
    assert "pass_rate" in md and "alice" in md and "bob" in md and "0.66" in md
    assert "None" in render_conflicts([]) and "agree" in render_conflicts([])


def test_write_index_emits_matrix_and_conflicts(tmp_path) -> None:
    res = build_index([_sub(tmp_path / "s1", "alice", 1.0), _sub(tmp_path / "s2", "bob", 0.66)])
    index_dir = write_index(tmp_path / "out", res)
    assert (index_dir / "matrix.csv").exists()
    assert "Conflicts" in (index_dir / "conflicts.md").read_text(encoding="utf-8")


def test_cli_exchange_index_empty_is_ok(tmp_path) -> None:
    # A fresh exchange (no submissions) is a valid state — write an empty index, exit 0.
    (tmp_path / "contributions").mkdir()
    result = CliRunner().invoke(
        app, ["exchange", "index", str(tmp_path / "contributions"), "--out", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.stdout
    assert "empty index" in result.stdout
    assert (tmp_path / "out" / "index" / "matrix.csv").exists()


def test_cli_exchange_index(tmp_path) -> None:
    root = tmp_path / "contributions"
    _sub(root / "a", "alice", 1.0)
    _sub(root / "b", "bob", 1.0)
    result = CliRunner().invoke(app, ["exchange", "index", str(root), "--out", str(tmp_path / "out")])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "out" / "index" / "matrix.csv").exists()
    assert "1 rows" in result.stdout or "2 rows" in result.stdout
