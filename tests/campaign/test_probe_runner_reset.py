"""Tests for the probe runner's out/-reset (stale-workspace fix, axis-1 v4)."""

from __future__ import annotations

from scripts.run_tool_call_fidelity import PROJECT_NAME, reset_out_dir, seed_probe_files


def test_reset_out_dir_clears_out_never_plan(tmp_path) -> None:
    project = tmp_path / PROJECT_NAME
    (project / "out").mkdir(parents=True)
    (project / "out" / "concat.txt").write_text("STALE", encoding="utf-8")
    (project / "plan").mkdir(parents=True)
    (project / "plan" / "seed.txt").write_text("input", encoding="utf-8")
    reset_out_dir(project)
    assert not (project / "out").exists()  # out/ cleared
    assert (project / "plan" / "seed.txt").read_text(encoding="utf-8") == "input"  # plan untouched


def test_seed_probe_files_resets_stale_out(tmp_path) -> None:
    """A stale out/ file (which would guard-block the model's write) is gone after
    seeding, while plan/ inputs are freshly written."""
    project = tmp_path / PROJECT_NAME
    (project / "out").mkdir(parents=True)
    (project / "out" / "concat.txt").write_text("STALE", encoding="utf-8")
    seed_probe_files(tmp_path, PROJECT_NAME)
    assert not (project / "out" / "concat.txt").exists()  # stale output removed
    assert (project / "plan" / "seed.txt").is_file()  # plan/ seeded fresh


def test_seed_files_are_lf_no_crlf_on_disk(tmp_path) -> None:
    """Seeds are the equality baseline — they must be exact LF bytes (no 0x0d),
    even on Windows, so byte-exact model output can match (determinism-probe)."""
    seed_probe_files(tmp_path, PROJECT_NAME)
    plan = tmp_path / PROJECT_NAME / "plan"
    for f in plan.iterdir():
        raw = f.read_bytes()
        assert b"\r" not in raw, f"{f.name} contains 0x0d (CRLF)"
    # and a known seed round-trips to its exact LF content
    assert (plan / "seed_a.txt").read_bytes() == b"apple\napricot\navocado\n"
