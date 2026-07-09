"""`agora contribute` — dry-run and --write behind the CLI (L2-1)."""

from __future__ import annotations

from typer.testing import CliRunner

from agora.cli import app
from tests.exchange._fixtures import DIGEST, make_records

runner = CliRunner()


def _write_run_dir(where):
    where.mkdir(parents=True, exist_ok=True)
    runs, tasks = make_records()
    (where / "run.jsonl").write_text("\n".join(r.model_dump_json() for r in runs), encoding="utf-8")
    (where / "tasks.jsonl").write_text("\n".join(t.model_dump_json() for t in tasks), encoding="utf-8")
    return where


def test_contribute_dry_run_ok(tmp_path) -> None:
    src = _write_run_dir(tmp_path / "run")
    result = runner.invoke(
        app,
        ["contribute", str(src), "--digest", DIGEST, "--battery", "standard-v1",
         "--contributor", "octocat", "--dest", str(tmp_path / "dist")],
    )
    assert result.exit_code == 0, result.stdout
    assert "dry-run OK" in result.stdout
    assert not (tmp_path / "dist").exists()  # dry-run writes nothing


def test_contribute_write_materializes(tmp_path) -> None:
    src = _write_run_dir(tmp_path / "run")
    result = runner.invoke(
        app,
        ["contribute", str(src), "--digest", DIGEST, "--dest", str(tmp_path / "dist"), "--write"],
    )
    assert result.exit_code == 0, result.stdout
    assert "written" in result.stdout
    assert list((tmp_path / "dist").rglob("manifest.yaml"))
