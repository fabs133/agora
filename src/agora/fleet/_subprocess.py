"""Shared helper for spawning the host Python interpreter under strict hygiene.

Every caller (inner tools, postconditions, one-off introspection) funnels through
:func:`run_host_python` so timeout behaviour, env whitelisting, and the
no-shell rule live in exactly one place.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SAFE_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "HOME",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "LOCALAPPDATA",
    "APPDATA",
)


@dataclass(frozen=True)
class RunResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def run_host_python(
    args: list[str],
    *,
    cwd: str | Path,
    timeout: float,
    extra_env: dict[str, str] | None = None,
) -> RunResult:
    """Invoke the orchestrator's interpreter with ``args`` under ``cwd``.

    Env is whitelist-reduced (Windows needs SYSTEMROOT for socket init etc.)
    plus ``DISCORD_TOKEN='dummy'`` and whatever ``extra_env`` adds. Never uses
    ``shell=True``. Always kills on timeout. stdout/stderr captured as text.
    """
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env.setdefault("DISCORD_TOKEN", "dummy")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (
            exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, (bytes, bytearray)) else ""
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (
            exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, (bytes, bytearray)) else ""
        )
        return RunResult(
            ok=False,
            returncode=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )
    return RunResult(
        ok=(proc.returncode == 0),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timed_out=False,
    )


def format_failure(result: RunResult, limit: int = 2000) -> str:
    """Format a failed :class:`RunResult` into a short diagnostic string."""
    if result.timed_out:
        return "TIMEOUT"
    tail_out = result.stdout[-limit:]
    tail_err = result.stderr[-limit:]
    return (
        f"exit {result.returncode}\n"
        f"--- stderr ---\n{tail_err}\n"
        f"--- stdout ---\n{tail_out}"
    ).strip()


__all__ = ["RunResult", "SAFE_ENV_KEYS", "format_failure", "run_host_python"]
