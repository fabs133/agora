"""run_check postcondition predicate (integration run 1)."""

from __future__ import annotations

import sys

from agora.plan.predicate_registry import build_predicate

PY = sys.executable


def _run(args: dict, work_dir, sink=None):
    pred = build_predicate("run_check", args)
    ctx = {"work_dir": str(work_dir)}
    if sink is not None:
        ctx["run_check_sink"] = sink
    return pred.evaluate(ctx)


def test_run_check_happy_path(tmp_path) -> None:
    sink: list = []
    passed, reason = _run(
        {"cmd": [PY, "-c", "print('pong')"], "expect_stdout_contains": "pong"},
        tmp_path, sink,
    )
    assert passed is True
    assert reason == ""
    rec = sink[0]
    assert rec["exit_code"] == 0
    assert rec["timed_out"] is False
    assert rec["passed"] is True
    assert rec["stdout"].strip() == "pong"


def test_run_check_resolves_bare_python_to_this_interpreter(tmp_path) -> None:
    """A flow's ``["python", ...]`` gate must run THIS venv, not whatever
    ``python`` PATH resolves to on a stranger's box (onboarding pre-mortem A3).
    The recorded cmd stays the portable original."""
    sink: list = []
    # sys.version_info is interpreter-specific; if the bare "python" were run via
    # PATH it could be a different interpreter (or absent). Asserting the printed
    # executable matches sys.executable proves the resolution.
    passed, reason = _run(
        {
            "cmd": ["python", "-c", "import sys; print(sys.executable)"],
            "expect_stdout_contains": sys.executable,
        },
        tmp_path, sink,
    )
    assert passed is True, reason
    assert sink[0]["cmd"][0] == "python"  # record keeps the portable original


def test_run_check_exit_mismatch_fails(tmp_path) -> None:
    passed, reason = _run({"cmd": [PY, "-c", "import sys; sys.exit(3)"]}, tmp_path)
    assert passed is False
    assert "exited 3" in reason and "expected 0" in reason


def test_run_check_timeout_edge(tmp_path) -> None:
    sink: list = []
    passed, reason = _run(
        {"cmd": [PY, "-c", "import time; time.sleep(5)"], "timeout_s": 0.5},
        tmp_path, sink,
    )
    assert passed is False
    assert "timed out" in reason
    assert sink[0]["timed_out"] is True
    assert sink[0]["exit_code"] is None
    assert sink[0]["passed"] is False


def test_run_check_stdout_contains_miss(tmp_path) -> None:
    sink: list = []
    passed, reason = _run(
        {"cmd": [PY, "-c", "print('nope')"], "expect_stdout_contains": "pong"},
        tmp_path, sink,
    )
    assert passed is False
    assert "did not contain 'pong'" in reason
    # Exit code was fine (0); the failure is the stdout miss.
    assert sink[0]["exit_code"] == 0
    assert sink[0]["passed"] is False


def test_run_check_truncation_marked(tmp_path) -> None:
    # Emit > 4 KB so the capture is truncated and flagged.
    sink: list = []
    passed, _ = _run(
        {"cmd": [PY, "-c", "print('x' * 10000)"], "expect_exit": 0},
        tmp_path, sink,
    )
    assert passed is True  # exit 0, no stdout expectation
    rec = sink[0]
    assert rec["stdout_truncated"] is True
    # head+tail: 2 KB each end + the marker line (F11) — bounded, not head-only.
    assert len(rec["stdout"].encode("utf-8")) <= 2 * 2048 + 64
    assert "bytes truncated ..." in rec["stdout"]
    assert rec["stderr_truncated"] is False


def test_run_check_truncation_retains_tail_marker(tmp_path) -> None:
    """F11: an oversized output keeps a DISTINCTIVE tail string verbatim — the
    head-only bound dropped it; head+tail keeps pytest's summary line."""
    sink: list = []
    passed, _ = _run(
        {"cmd": [PY, "-c", "print('A' * 6000 + 'DISTINCTIVE_TAIL_MARKER_7')"],
         "expect_exit": 0},
        tmp_path, sink,
    )
    assert passed is True
    rec = sink[0]
    assert rec["stdout_truncated"] is True
    assert "DISTINCTIVE_TAIL_MARKER_7" in rec["stdout"]  # tail survived verbatim
    assert "bytes truncated ..." in rec["stdout"]


def test_run_check_stdin_feeds_process(tmp_path) -> None:
    """stdin is delivered without a shell pipe (the P7 acceptance mechanism)."""
    passed, _ = _run(
        {
            "cmd": [PY, "-c", "import sys; print('got:' + sys.stdin.readline().strip())"],
            "stdin": "!ping\n",
            "expect_stdout_contains": "got:!ping",
        },
        tmp_path,
    )
    assert passed is True


def test_run_check_cwd_escape_fails_closed(tmp_path) -> None:
    passed, reason = _run({"cmd": [PY, "-c", "pass"], "cwd": "../.."}, tmp_path)
    assert passed is False
    assert "escapes work_dir" in reason


def test_run_check_non_list_cmd_fails_closed(tmp_path) -> None:
    passed, reason = _run({"cmd": "python -c pass"}, tmp_path)
    assert passed is False
    assert "argv" in reason


def test_run_check_names_are_unique_per_distinct_check() -> None:
    # Same argv, different stdin/expected → distinct predicate names.
    a = build_predicate("run_check", {"cmd": [PY, "-m", "echobot"], "stdin": "!ping\n"})
    b = build_predicate("run_check", {"cmd": [PY, "-m", "echobot"], "stdin": "!echo hi\n"})
    assert a.name != b.name
