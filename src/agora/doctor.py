"""One preflight: ``agora doctor``.

Every entry point calls THIS module — no entry point reimplements a health
check (integration-hardening Stage 4). Each check returns a structured
:class:`CheckResult` with a one-line red/green verdict and a fix hint; the
:func:`report` helper prints them and yields a non-zero exit code on any red.

Library-clean: every check RECEIVES the endpoints/values it needs (composition
roots inject them from Settings). This module never reads ``os.environ`` or
``Settings`` itself, so it stays off the config-import allowlist.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    """One preflight check outcome. ``hint`` is shown only when ``ok`` is False.

    ``skipped`` is a THIRD state, deliberately not folded into ``ok``: a check
    that never ran is not a check that passed. A run with the Matrix surface off
    should report ``[SKIP] conduit`` — reading ``[ OK ] conduit`` there would
    claim a homeserver was verified when none was contacted.
    """

    name: str
    ok: bool
    detail: str
    hint: str = ""
    skipped: bool = False


def _get_json(url: str, timeout: float) -> Any:
    """GET a URL and parse JSON. Kept as one small seam so tests can monkeypatch
    the network for every Ollama check at once."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (local dev URL)
        return json.loads(resp.read())


# ------------------------------------------------------------------- Ollama


def check_ollama_reachable(base_url: str) -> CheckResult:
    """Ollama daemon answers ``/api/version``."""
    try:
        data = _get_json(f"{base_url.rstrip('/')}/api/version", 3)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "ollama",
            False,
            f"unreachable at {base_url} ({type(exc).__name__})",
            hint="start it per OLLAMA.md (bare `ollama serve`, OLLAMA_MODELS set)",
        )
    return CheckResult("ollama", True, f"reachable at {base_url} (version {data.get('version', '?')})")


def ollama_missing_models(tags: dict[str, Any], required: Iterable[str]) -> list[str]:
    """Required model tags (``name:tag``) absent from an ``/api/tags`` payload."""
    present = {m.get("name", "") for m in (tags.get("models") or [])}
    return [m for m in required if m not in present]


def check_ollama_models(base_url: str, required: Sequence[str]) -> CheckResult:
    """The active cast's models are present in ``/api/tags``."""
    if not required:
        return CheckResult("ollama-models", True, "no resident models required by the active cast")
    try:
        tags = _get_json(f"{base_url.rstrip('/')}/api/tags", 5)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "ollama-models", False, f"/api/tags failed ({type(exc).__name__})",
            hint="is the Ollama daemon up?",
        )
    missing = ollama_missing_models(tags, required)
    if missing:
        return CheckResult(
            "ollama-models", False, f"missing: {', '.join(missing)}",
            hint=f"ollama pull {missing[0]}",
        )
    return CheckResult("ollama-models", True, f"all {len(required)} cast model(s) present")


async def check_vram(model: str, base_url: str, safety_margin_mib: int) -> CheckResult:
    """Enough free VRAM for ``model`` (existing vram.py fit math). An unavailable
    probe is GREEN — we never block a run on an unknown (matches vram.py policy)."""
    from agora.fleet.vram import check_model_fits

    try:
        chk = await check_model_fits(model, base_url, safety_margin_mib=safety_margin_mib)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("vram", True, f"probe unavailable ({type(exc).__name__}) - not blocking")
    if chk.fits:
        return CheckResult("vram", True, chk.reason)
    return CheckResult("vram", False, chk.reason, hint="free VRAM or pick a smaller model")


# ------------------------------------------------------------------- Conduit


async def check_conduit(homeserver: str, user_id: str, password: str) -> CheckResult:
    """Conduit is reachable AND the system account can log in."""
    if not password:
        return CheckResult(
            "conduit", False, f"cannot verify login for {user_id} (no password set)",
            hint="set AGORA_MATRIX_PASSWORD (see .env.example)",
        )
    from agora.matrix.client import AgoraMatrixClient

    client = AgoraMatrixClient(homeserver=homeserver, user_id=user_id)
    try:
        # A preflight must never hang: bound the login so a down homeserver
        # produces a fast red line instead of blocking on the socket.
        await asyncio.wait_for(client.login(password), timeout=8.0)
    except TimeoutError:
        return CheckResult(
            "conduit", False, f"login to {homeserver} timed out (8s)",
            hint="is Conduit up (docker compose up -d), or is port 6167 taken by another process?",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "conduit", False, f"login failed for {user_id} at {homeserver} ({type(exc).__name__})",
            hint="is Conduit up (docker compose up -d) and the account registered? (or port 6167 in use)",
        )
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001, S110
            pass
    return CheckResult("conduit", True, f"login OK as {user_id} at {homeserver}")


# ------------------------------------------------------------------- local env


def check_workspace_git(repo_root: str | Path, work_dir: str | Path) -> CheckResult:
    """We are inside a git work tree and the workspace root is creatable/writable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        in_tree = proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception as exc:  # noqa: BLE001
        return CheckResult("workspace", False, f"git check failed ({type(exc).__name__})",
                           hint="run from inside the cloned repo")
    if not in_tree:
        return CheckResult("workspace", False, f"{repo_root} is not a git work tree",
                           hint="run from inside the cloned repo")
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("workspace", False, f"workspace {work_dir} not writable ({type(exc).__name__})",
                           hint="check filesystem permissions for the work dir")
    return CheckResult("workspace", True, f"git work tree OK; workspace {work_dir} writable")


def check_plantuml(url: str) -> CheckResult:
    """(--dev) The PlantUML render server answers. Dev-only: diagram tooling."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        urllib.request.urlopen(req, timeout=3)  # noqa: S310 (local dev URL)
    except urllib.error.HTTPError:
        # Any HTTP response means the server is up (HEAD may be 405).
        return CheckResult("plantuml", True, f"reachable at {url}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("plantuml", False, f"unreachable at {url} ({type(exc).__name__})",
                           hint="start the plantuml-server container (dev only)")
    return CheckResult("plantuml", True, f"reachable at {url}")


# ------------------------------------------------------------------- orchestration


async def run_checks(
    *,
    ollama_base_url: str,
    required_models: Sequence[str] = (),
    vram_model: str,
    vram_safety_margin_mib: int,
    repo_root: str | Path,
    work_dir: str | Path,
    homeserver: str | None = None,
    matrix_user_id: str | None = None,
    matrix_password: str | None = None,
    dev: bool = False,
    plantuml_url: str | None = None,
) -> list[CheckResult]:
    """Run the standard preflight and return every result (order = display order).

    ``homeserver`` set ⇒ the Conduit reachable+login check runs. ``dev`` ⇒ the
    PlantUML check is appended.
    """
    results: list[CheckResult] = [
        check_ollama_reachable(ollama_base_url),
        check_ollama_models(ollama_base_url, tuple(required_models)),
        await check_vram(vram_model, ollama_base_url, vram_safety_margin_mib),
    ]
    if homeserver is not None:
        results.append(await check_conduit(homeserver, matrix_user_id or "", matrix_password or ""))
    results.append(check_workspace_git(repo_root, work_dir))
    if dev:
        results.append(check_plantuml(plantuml_url or ""))
    return results


def skipped(name: str, reason: str) -> CheckResult:
    """A check that did not run, and says so.

    Not a green: nothing was verified. Used where a dependency is genuinely
    absent from a run's path (e.g. Conduit when the Matrix surface is off), so
    the report neither fails on it nor pretends it was checked.
    """
    return CheckResult(name, ok=True, detail=f"skipped ({reason})", skipped=True)


def format_line(r: CheckResult) -> str:
    # ASCII-only separator: this output must render in a bare cp1252 Windows
    # console (the doctor runs before force_utf8_stdio would be in play).
    tag = "SKIP" if r.skipped else (" OK " if r.ok else "FAIL")
    line = f"[{tag}] {r.name}: {r.detail}"
    if not r.ok and r.hint:
        line += f"  -> {r.hint}"
    return line


def report(results: Sequence[CheckResult], echo: Callable[[str], None] = print) -> int:
    """Print each verdict; return 0 when all green, 1 when any check is red."""
    echo("== agora doctor ==")
    for r in results:
        echo(format_line(r))
    reds = [r for r in results if not r.ok and not r.skipped]
    if reds:
        echo(f"\n{len(reds)} check(s) FAILED - fix the items marked FAIL above.")
        return 1
    n_skipped = sum(1 for r in results if r.skipped)
    echo(f"\nall checks passed.{f' ({n_skipped} skipped)' if n_skipped else ''}")
    return 0


def preflight_or_die(results: Sequence[CheckResult], echo: Callable[[str], None] = print) -> None:
    """Report and raise ``SystemExit(1)`` on any red — for fail-fast entry points."""
    if report(results, echo) != 0:
        raise SystemExit(1)
