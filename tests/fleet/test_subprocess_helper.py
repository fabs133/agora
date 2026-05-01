"""Unit tests for the shared subprocess helper."""

from __future__ import annotations

from pathlib import Path

from agora.fleet._subprocess import format_failure, run_host_python


def test_run_host_python_echoes_zero_on_success(tmp_path: Path) -> None:
    result = run_host_python(["-c", "print('hi')"], cwd=tmp_path, timeout=10.0)
    assert result.ok is True
    assert result.returncode == 0
    assert "hi" in result.stdout
    assert result.timed_out is False


def test_run_host_python_nonzero_on_raise(tmp_path: Path) -> None:
    result = run_host_python(["-c", "raise SystemExit(2)"], cwd=tmp_path, timeout=10.0)
    assert result.ok is False
    assert result.returncode == 2


def test_run_host_python_timeout(tmp_path: Path) -> None:
    result = run_host_python(
        ["-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=0.5
    )
    assert result.timed_out is True
    assert result.ok is False


def test_run_host_python_seeds_dummy_discord_token(tmp_path: Path) -> None:
    result = run_host_python(
        ["-c", "import os; print(os.environ['DISCORD_TOKEN'])"],
        cwd=tmp_path,
        timeout=10.0,
    )
    assert result.ok is True
    assert "dummy" in result.stdout


def test_run_host_python_does_not_leak_parent_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGORA_SECRET_LEAK_PROBE", "SECRET")
    result = run_host_python(
        [
            "-c",
            "import os; print(os.environ.get('AGORA_SECRET_LEAK_PROBE', 'absent'))",
        ],
        cwd=tmp_path,
        timeout=10.0,
    )
    assert result.ok is True
    assert "absent" in result.stdout
    assert "SECRET" not in result.stdout


def test_run_host_python_cwd_is_honored(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    result = run_host_python(
        [
            "-c",
            "from pathlib import Path; print(Path('marker.txt').exists())",
        ],
        cwd=tmp_path,
        timeout=10.0,
    )
    assert result.ok is True
    assert "True" in result.stdout


def test_run_host_python_extra_env_overrides_default(tmp_path: Path) -> None:
    result = run_host_python(
        ["-c", "import os; print(os.environ['DISCORD_TOKEN'])"],
        cwd=tmp_path,
        timeout=10.0,
        extra_env={"DISCORD_TOKEN": "override"},
    )
    assert result.ok is True
    assert "override" in result.stdout


def test_format_failure_handles_timeout() -> None:
    from agora.fleet._subprocess import RunResult

    result = RunResult(ok=False, returncode=-1, stdout="", stderr="", timed_out=True)
    assert format_failure(result) == "TIMEOUT"


def test_format_failure_includes_tails() -> None:
    from agora.fleet._subprocess import RunResult

    result = RunResult(
        ok=False, returncode=1, stdout="out-tail", stderr="err-tail", timed_out=False
    )
    msg = format_failure(result)
    assert "exit 1" in msg
    assert "out-tail" in msg
    assert "err-tail" in msg
