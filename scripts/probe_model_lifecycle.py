"""Cross-platform model-lifecycle probe (stdlib-only).

Wraps a runner command, streams its stdout+stderr to ``run.log``, and
samples ``nvidia-smi`` + ``ollama ps`` at a regular interval into
``timeline.log``. Snapshots are also taken pre-run and post-run so a
reviewer can see the load → resident → teardown trajectory without
running the workload themselves.

The key invariant: the wrapped process is **awaited to completion** —
:func:`subprocess.Popen.wait` only returns after natural exit (or after
the hard wall-clock cap fires, in which case the process is terminated
and the run is flagged ``HANG`` rather than silently truncated).

Usage (everything after ``--`` is the runner command + its env+args):

    python scripts/probe_model_lifecycle.py --out runs_out/32b -- \\
        AGORA_PROFILE=qwen-coder-32b-p40 python scripts/run_discord_bot_test.py

Output files (under ``--out``):
  - ``run.log``         — full runner stdout+stderr, line-buffered
  - ``timeline.log``    — periodic ISO-ts + ollama ps + nvidia-smi samples
  - ``snapshot_pre.txt``  — pre-run ollama ps + nvidia-smi
  - ``snapshot_post.txt`` — post-run ollama ps + nvidia-smi
  - ``probe_meta.json``   — start/end ts, exit code, wall-clock, cap-hit
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

NVIDIA_QUERY = "memory.used,memory.total,memory.free,utilization.gpu"


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_cmd(args: list[str], timeout: float = 5.0) -> str:
    """Best-effort capture of a short command. Returns trimmed output or an error tag."""
    if shutil.which(args[0]) is None:
        return f"[{args[0]} not on PATH]"
    try:
        cp = subprocess.run(  # noqa: S603 — args are constants from this script
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[{args[0]} timed out after {timeout}s]"
    except OSError as exc:
        return f"[{args[0]} failed: {exc}]"
    body = (cp.stdout or "") + (cp.stderr or "")
    return body.strip() or f"[{args[0]} exited {cp.returncode} with no output]"


def _ollama_ps() -> str:
    return _run_cmd(["ollama", "ps"])


def _nvidia_smi() -> str:
    return _run_cmd(
        [
            "nvidia-smi",
            f"--query-gpu={NVIDIA_QUERY}",
            "--format=csv,noheader",
        ]
    )


def _snapshot(label: str) -> str:
    return (
        f"=== {label} @ {_now_iso()} ===\n"
        f"--- nvidia-smi ({NVIDIA_QUERY}) ---\n"
        f"{_nvidia_smi()}\n"
        f"--- ollama ps ---\n"
        f"{_ollama_ps()}\n"
    )


def _parse_env_pairs(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    """Split a leading ``KEY=VALUE`` env-var prefix from the runner argv.

    Stops at the first token that is not a valid ``KEY=VALUE`` literal,
    so ``KEY=val python script.py --foo`` is correctly partitioned into
    ``{KEY: val}`` and ``['python', 'script.py', '--foo']``.
    """
    env: dict[str, str] = {}
    for i, tok in enumerate(tokens):
        if "=" in tok and tok.split("=", 1)[0].isidentifier():
            key, val = tok.split("=", 1)
            env[key] = val
        else:
            return env, tokens[i:]
    return env, []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wrap a runner, sample ollama ps + nvidia-smi on an interval, "
        "await the process to natural completion (or hard cap → HANG).",
    )
    parser.add_argument("--out", required=True, help="Output directory (created).")
    parser.add_argument(
        "--interval",
        type=float,
        default=8.0,
        help="Seconds between timeline samples (default 8).",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=1800.0,
        help="Hard wall-clock cap; on hit, snapshots are taken and the "
        "child is terminated and the run is flagged HANG (default 1800).",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Everything after `--`: optional KEY=VAL env pairs then the "
        "runner command and its args.",
    )
    args = parser.parse_args()

    if not args.command:
        parser.error("missing runner command after `--`")
    # argparse REMAINDER may include the literal '--' separator.
    cmd_tokens = args.command[1:] if args.command and args.command[0] == "--" else args.command
    env_overlay, cmd = _parse_env_pairs(cmd_tokens)
    if not cmd:
        parser.error("after stripping KEY=VAL env tokens, no runner command remained")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_log = out_dir / "run.log"
    timeline_log = out_dir / "timeline.log"
    snap_pre = out_dir / "snapshot_pre.txt"
    snap_post = out_dir / "snapshot_post.txt"
    meta_path = out_dir / "probe_meta.json"

    # Pre-run snapshot.
    snap_pre.write_text(_snapshot("PRE-RUN"), encoding="utf-8")

    full_env = {**os.environ, **env_overlay}
    start_wall = time.monotonic()
    start_iso = _now_iso()

    cap_hit = False
    exit_code: int | None = None
    print(
        f"[probe] launching: {' '.join(cmd)}\n"
        f"[probe] env overlay: {env_overlay}\n"
        f"[probe] out_dir={out_dir}\n"
        f"[probe] interval={args.interval}s, max_seconds={args.max_seconds}s",
        flush=True,
    )

    # Buffer the timeline samples in memory and flush after the process
    # ends — keeps overhead off the hot path.
    timeline_lines: list[str] = []

    with run_log.open("w", encoding="utf-8", errors="replace", buffering=1) as log_fh:
        log_fh.write(f"[probe] start={start_iso} cmd={cmd!r} env_overlay={env_overlay!r}\n")
        log_fh.flush()
        # Merge stderr → stdout so log order matches what a human sees.
        proc = subprocess.Popen(  # noqa: S603 — caller supplies argv tokens
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=full_env,
            cwd=os.getcwd(),
        )

        while proc.poll() is None:
            elapsed = time.monotonic() - start_wall
            if elapsed >= args.max_seconds:
                cap_hit = True
                print(f"[probe] HARD CAP {args.max_seconds}s exceeded, terminating", flush=True)
                _terminate(proc)
                break
            sample = (
                f"--- t={elapsed:7.1f}s wall={_now_iso()} ---\n"
                f"nvidia-smi: {_nvidia_smi()}\n"
                f"ollama ps:\n{_ollama_ps()}\n"
            )
            timeline_lines.append(sample)
            time.sleep(args.interval)

        # proc.wait() awaits the process to natural completion (or the
        # terminate above). This is the await-to-completion guarantee.
        exit_code = proc.wait()
        log_fh.write(
            f"\n[probe] end={_now_iso()} exit_code={exit_code} cap_hit={cap_hit}\n"
        )

    end_wall = time.monotonic()
    wall_seconds = end_wall - start_wall

    snap_post.write_text(_snapshot("POST-RUN"), encoding="utf-8")
    timeline_log.write_text("".join(timeline_lines), encoding="utf-8")

    meta = {
        "start_iso": start_iso,
        "end_iso": _now_iso(),
        "wall_seconds": round(wall_seconds, 2),
        "exit_code": exit_code,
        "cap_hit": cap_hit,
        "interval_seconds": args.interval,
        "max_seconds": args.max_seconds,
        "command": cmd,
        "env_overlay": env_overlay,
        "samples": len(timeline_lines),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    status = "HANG" if cap_hit else f"exit={exit_code}"
    print(
        f"[probe] done in {wall_seconds:.1f}s ({status}); "
        f"see {run_log}, {timeline_log}, {snap_pre}, {snap_post}, {meta_path}",
        flush=True,
    )
    # Propagate child's exit status so CI / shells can branch on it.
    if cap_hit:
        return 124  # standard "timeout" exit code on Unix
    return int(exit_code or 0)


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort graceful → forceful termination.

    Cross-platform: ``terminate()`` sends SIGTERM on POSIX and ``CTRL_BREAK_EVENT``
    isn't suitable for our case so we just call ``terminate()``; then escalate
    to ``kill()`` if the child hasn't exited within 10 s.
    """
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    # Reference signal so static analysers don't strip the import on
    # platforms where we'd actually want SIGINT later.
    _ = signal.SIGTERM


if __name__ == "__main__":
    sys.exit(main())
