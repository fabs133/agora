"""``run_check`` postcondition predicate — execute a command, gate on its result.

Integration run 1 (docs/design/project-phases.md) needs one mechanism that
covers phases 5/6/7: run a command in the task workspace and assert its exit
code (and optionally that its stdout contains a string). This is the test- and
acceptance-execution gate the axis-1 probe never had.

Design constraints:
  - **argv only, no shell** — ``shell=False``, ``cmd`` is a literal argv list.
    That is the network guarantee for run 1: no shell means no ``$(...)`` /
    pipes / redirects, and we assert no resolver use by simply never handing
    the command to a shell. OS-level network isolation is out of scope here.
  - **cwd pinned under the workspace** — ``cwd`` is workspace-relative and may
    not escape ``work_dir``.
  - **bounded capture** — stdout and stderr are each truncated to 4 KB and the
    truncation is flagged, then appended to ``ctx["run_check_sink"]`` so the
    (watchlisted, length-prone) pytest output reaches the task record.

Registered via the standard ``@register_predicate`` decorator; imported by the
registry at its module bottom, exactly like ``probe_predicates``.

Findings: the bounded capture keeps the informative tail of a failure so the
repair oracle isn't starved (**F11** — head+tail, not head-only). This same
predicate is the round-trip target of a handoff Verification record: **F20**
serializes each gate as a COMPLETE ``run_check`` spec (cmd + stdin + expectation)
precisely so a future phase-0 re-validation can feed it straight back into
``build_predicate("run_check", spec)`` and re-execute it verbatim.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agora.core.contract import Predicate, make_predicate
from agora.plan.predicate_registry import register_predicate

#: Per-stream capture ceiling. Generous on purpose — pytest tracebacks are the
#: channel's first real length stress (project-phases watchlist).
_CAPTURE_LIMIT_BYTES = 4096
#: When over the ceiling, keep this many bytes from EACH end (head + tail).
_HEAD_TAIL_BYTES = 2048


def _bound(text: str) -> tuple[str, bool]:
    """Return ``text`` head+tail truncated with a marked gap + a truncated flag.

    When the encoded length exceeds :data:`_CAPTURE_LIMIT_BYTES`, keep the first
    and last :data:`_HEAD_TAIL_BYTES` bytes joined by a
    ``\\n[... N bytes truncated ...]\\n`` marker (F11): pytest's most diagnostic
    lines are the TAIL summary, which the old head-only bound dropped first.
    Bounds by encoded bytes, decoding back with ``replace`` so a split multibyte
    char can't raise."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= _CAPTURE_LIMIT_BYTES:
        return text, False
    dropped = len(raw) - 2 * _HEAD_TAIL_BYTES
    head = raw[:_HEAD_TAIL_BYTES].decode("utf-8", "replace")
    tail = raw[-_HEAD_TAIL_BYTES:].decode("utf-8", "replace")
    return f"{head}\n[... {dropped} bytes truncated ...]\n{tail}", True


@register_predicate("run_check")
def postcond_run_check(
    cmd: list[str],
    cwd: str = ".",
    timeout_s: float = 30.0,
    expect_exit: int = 0,
    expect_stdout_contains: str = "",
    stdin: str = "",
) -> Predicate:
    """Run ``cmd`` (argv, no shell) under ``work_dir/cwd`` and gate on its result.

    Passes iff the process exits within ``timeout_s`` with code ``expect_exit``
    and — when ``expect_stdout_contains`` is non-empty — its stdout contains that
    substring. ``stdin`` (when non-empty) is fed to the process's standard input,
    so a headless acceptance check (``!ping`` → ``pong``) needs no shell pipe.
    Fails closed on a missing/invalid cwd, a non-list ``cmd``, a timeout, an
    exit-code mismatch, or a stdout miss. Every invocation records a capture dict
    into ``ctx["run_check_sink"]`` when present.
    """

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        if not cmd or not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
            return (False, "run_check: cmd must be a non-empty list of strings (argv)")

        base = Path(work_dir).resolve()
        run_cwd = (base / cwd).resolve()
        try:
            run_cwd.relative_to(base)
        except ValueError:
            return (False, f"run_check: cwd {cwd!r} escapes work_dir")
        if not run_cwd.is_dir():
            return (False, f"run_check: cwd {cwd!r} is not a directory under work_dir")

        record: dict[str, Any] = {
            "cmd": list(cmd),
            "cwd": cwd,
            "expect_exit": expect_exit,
            "expect_stdout_contains": expect_stdout_contains,
        }
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(run_cwd),
                input=stdin if stdin else None,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,  # argv only — the run-1 no-network guarantee
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out, out_trunc = _bound(exc.stdout or "" if isinstance(exc.stdout, str) else "")
            err, err_trunc = _bound(exc.stderr or "" if isinstance(exc.stderr, str) else "")
            record.update(
                exit_code=None, timed_out=True, stdout=out, stderr=err,
                stdout_truncated=out_trunc, stderr_truncated=err_trunc, passed=False,
            )
            _sink(ctx, record)
            return (False, f"run_check: {' '.join(cmd)} timed out after {timeout_s}s")
        except (OSError, ValueError) as exc:
            record.update(
                exit_code=None, timed_out=False, stdout="", stderr=str(exc),
                stdout_truncated=False, stderr_truncated=False, passed=False,
            )
            _sink(ctx, record)
            return (False, f"run_check: could not execute {cmd!r}: {exc}")

        out, out_trunc = _bound(proc.stdout or "")
        err, err_trunc = _bound(proc.stderr or "")
        exit_ok = proc.returncode == expect_exit
        stdout_ok = (not expect_stdout_contains) or (expect_stdout_contains in (proc.stdout or ""))
        passed = exit_ok and stdout_ok
        record.update(
            exit_code=proc.returncode, timed_out=False, stdout=out, stderr=err,
            stdout_truncated=out_trunc, stderr_truncated=err_trunc, passed=passed,
        )
        _sink(ctx, record)

        if passed:
            return (True, "")
        if not exit_ok:
            return (
                False,
                f"run_check: {' '.join(cmd)} exited {proc.returncode}, expected "
                f"{expect_exit}. stderr: {err.strip()[:200]}",
            )
        return (
            False,
            f"run_check: {' '.join(cmd)} stdout did not contain "
            f"{expect_stdout_contains!r}. stdout: {out.strip()[:200]}",
        )

    # Name must be UNIQUE per distinct check: two run_checks with the same argv
    # but different stdin / expected-output (the P7 acceptance triple all run
    # ``python -m echobot``) must not collapse to one predicate name, or their
    # per-predicate outcomes alias in provenance. Append a short stable hash of
    # the full arg tuple (deterministic — no RNG).
    import hashlib

    safe = "_".join(cmd) if isinstance(cmd, list) else str(cmd)
    for ch in " /\\.:":
        safe = safe.replace(ch, "_")
    disc = repr((list(cmd), cwd, timeout_s, expect_exit, expect_stdout_contains, stdin))
    suffix = hashlib.sha1(disc.encode("utf-8")).hexdigest()[:6]
    name = f"run_check_{safe}"[:53] + "_" + suffix
    return make_predicate(name, name, check)


def _sink(ctx: dict[str, Any], record: dict[str, Any]) -> None:
    """Append ``record`` to the run_check capture sink if the runtime wired one."""
    sink = ctx.get("run_check_sink")
    if isinstance(sink, list):
        sink.append(record)


__all__ = ["postcond_run_check"]
