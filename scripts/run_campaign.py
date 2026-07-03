"""Sequential campaign harness: run a multi-run sweep one model-load at a time.

Drives a campaign YAML (see campaigns/axis-1-tool-call-fidelity.yaml). Each run
is launched as a child process via scripts/probe_model_lifecycle.py (reused, not
reinvented) so every run gets its own run.log + GPU/ollama-ps timeline +
wall-clock cap. Between runs that target different models, an eviction protocol
unloads the outgoing model and pre-warms the incoming one so VRAM never holds
two models at once.

Usage:

    python scripts/run_campaign.py campaigns/axis-1-tool-call-fidelity.yaml
    python scripts/run_campaign.py --dry-run campaigns/axis-1-tool-call-fidelity.yaml

Resume: with `defaults.resume: true`, runs whose per-run run.jsonl already
exists under <output_dir>/<id>/ are skipped. SIGINT finishes the current run
(its child is in its own process group and is NOT killed) then stops without
starting the next; a resume hint is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.fleet.profiles import ProfileSet, load_profiles  # noqa: E402
from agora.plan.harness import force_utf8_stdio  # noqa: E402

#: Maps a probe flow path to the runner script that executes it. Keeps the
#: harness probe-agnostic without auto-discovering runners.
PROBE_RUNNERS: dict[str, str] = {
    "flows/tool-call-fidelity.plan.yaml": "scripts/run_tool_call_fidelity.py",
}

EVICTION_POLL_CAP_SECONDS = 30.0


# ------------------------------------------------------------------ schema


class Arm(BaseModel):
    model_config = {"extra": "forbid"}

    scaffolding: Literal["lean", "rich"] = "rich"
    strictness: Literal["strict", "permissive"] = "strict"


class CampaignDefaults(BaseModel):
    model_config = {"extra": "forbid"}

    params: dict[str, Any] = Field(default_factory=dict)
    output_dir: str
    resume: bool = True
    # Forwarded to each run as AGORA_REVIEW_TIMEOUT_SECONDS. The probe completes
    # without a human review, so a short value stops the orchestrator's REVIEW
    # phase from idling the full default (300s) between task completion and
    # phase advance. None → leave the runner's own default in place.
    review_timeout_seconds: float | None = None


class CampaignRun(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    probe: str
    profile: str
    arm: Arm
    repeat: int
    # Per-run override of defaults.params (merged over them). Optional.
    params: dict[str, Any] | None = None
    # Per-model prompting strategy (axis-1 v2). None ⇒ control cell: no wrapper
    # is constructed, byte-identical to v1. Non-null names are validated against
    # the strategy registry at load time (see load_campaign).
    strategy: str | None = None


class Campaign(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = 1
    name: str
    description: str = ""
    defaults: CampaignDefaults
    runs: list[CampaignRun]


def load_campaign(path: str | Path) -> Campaign:
    import yaml

    from agora.fleet.strategies import STRATEGIES

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    campaign = Campaign.model_validate(raw)
    # Reject unknown strategy names loudly at load — not at run 23 of 40.
    for run in campaign.runs:
        if run.strategy is not None and run.strategy not in STRATEGIES:
            raise ValueError(
                f"run {run.id!r}: unknown strategy {run.strategy!r}; "
                f"known: {sorted(STRATEGIES)}"
            )
    return campaign


# ------------------------------------------------------------------ pure helpers


def expand_plan(campaign: Campaign) -> list[dict[str, Any]]:
    """Resolve each run into a flat plan dict (defaults.params merged with overrides).

    Declared order is preserved (the YAML is already minimized-swap ordered).
    """
    plan: list[dict[str, Any]] = []
    base_params = dict(campaign.defaults.params)
    for run in campaign.runs:
        params = {**base_params, **(run.params or {})}
        plan.append(
            {
                "id": run.id,
                "probe": run.probe,
                "profile": run.profile,
                "arm": run.arm.model_dump(),
                "repeat": run.repeat,
                "params": params,
                "strategy": run.strategy,
                "review_timeout_seconds": campaign.defaults.review_timeout_seconds,
            }
        )
    return plan


def resume_filter(
    plan: list[dict[str, Any]], done_ids: set[str]
) -> list[dict[str, Any]]:
    """Return the runs NOT already completed (by id)."""
    return [run for run in plan if run["id"] not in done_ids]


def scan_done(output_dir: str | Path) -> set[str]:
    """Set of run ids whose per-run run.jsonl exists + parses under output_dir.

    Resume key: the per-run directory name IS the run id, and a valid run.jsonl
    inside it marks the tuple as done.
    """
    out = Path(output_dir)
    done: set[str] = set()
    if not out.is_dir():
        return done
    for child in out.iterdir():
        if not child.is_dir():
            continue
        run_jsonl = child / "run.jsonl"
        if not run_jsonl.is_file():
            continue
        try:
            line = run_jsonl.read_text(encoding="utf-8").strip().splitlines()
            if line and json.loads(line[0]):
                done.add(child.name)
        except (OSError, json.JSONDecodeError):
            continue
    return done


def build_env(run: dict[str, Any], run_dir: str | Path) -> dict[str, str]:
    """Construct the per-run env overlay: profile + param overrides + output dir."""
    env: dict[str, str] = {
        "AGORA_PROFILE": run["profile"],
        "AGORA_RUN_OUTPUT_DIR": str(run_dir),
    }
    params = run.get("params") or {}
    mapping = {
        "temperature": "AGORA_LLM_TEMPERATURE",
        "seed": "AGORA_LLM_SEED",
        "num_ctx": "AGORA_LLM_NUM_CTX",
        "max_tokens": "AGORA_LLM_MAX_TOKENS",
    }
    for key, env_name in mapping.items():
        if key in params and params[key] is not None:
            env[env_name] = str(params[key])
    # Propagate the per-run arm so the probe runner records it in run.jsonl.
    # Without this the observer defaults to ArmSpec() (rich/strict) for every
    # run and the campaign's lean/rich dimension never reaches the data.
    arm = run.get("arm") or {}
    if arm.get("scaffolding"):
        env["AGORA_ARM_SCAFFOLDING"] = str(arm["scaffolding"])
    if arm.get("strictness"):
        env["AGORA_ARM_STRICTNESS"] = str(arm["strictness"])
    # Per-model prompting strategy (axis-1 v2). Emitted only when set, so
    # control cells (strategy=None) carry no AGORA_STRATEGY and the runner
    # constructs no wrapper — byte-identical to v1.
    strategy = run.get("strategy")
    if strategy:
        env["AGORA_STRATEGY"] = str(strategy)
    # Short review timeout so the REVIEW phase doesn't idle the runner for the
    # full default (300s) waiting on a human poll that never comes in a sweep.
    rts = run.get("review_timeout_seconds")
    if rts is not None:
        env["AGORA_REVIEW_TIMEOUT_SECONDS"] = str(rts)
    return env


def build_probe_command(
    run: dict[str, Any], run_dir: str | Path, *, max_seconds: float = 1800.0
) -> list[str]:
    """The argv for launching one run via scripts/probe_model_lifecycle.py."""
    runner = PROBE_RUNNERS.get(run["probe"])
    if runner is None:
        raise ValueError(
            f"no runner registered for probe {run['probe']!r}; "
            f"known: {sorted(PROBE_RUNNERS)}"
        )
    env = build_env(run, run_dir)
    env_tokens = [f"{k}={v}" for k, v in env.items()]
    return [
        sys.executable,
        str(REPO_ROOT / "scripts" / "probe_model_lifecycle.py"),
        "--out",
        str(run_dir),
        "--max-seconds",
        str(max_seconds),
        "--",
        *env_tokens,
        sys.executable,
        str(REPO_ROOT / runner),
    ]


def model_for_profile(profile_name: str, profiles: ProfileSet) -> str:
    """Resolve a profile name to its bare Ollama model tag (no ``ollama/`` prefix)."""
    model = profiles.select(profile_name).model
    return model.removeprefix("ollama/")


# ------------------------------------------------------------------ Ollama control


class EvictionTimeout(RuntimeError):
    """Raised when an outgoing model does not unload within the poll cap."""


class OllamaControl:
    """Thin HTTP client for the daemon controls the campaign needs.

    Injectable so the eviction protocol is unit-testable with a fake.
    """

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict[str, Any], timeout: float = 30.0) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, timeout: float = 10.0) -> Any:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def reachable(self) -> bool:
        try:
            self._get("/api/version", timeout=5.0)
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False

    def list_local(self) -> set[str]:
        """Model tags present locally (GET /api/tags)."""
        data = self._get("/api/tags")
        return {m.get("name", "") for m in data.get("models", [])}

    def list_running(self) -> set[str]:
        """Model tags currently resident (GET /api/ps)."""
        data = self._get("/api/ps")
        return {m.get("name", "") for m in data.get("models", [])}

    def evict(self, model: str) -> None:
        """Request immediate unload (POST /api/generate keep_alive=0)."""
        self._post("/api/generate", {"model": model, "keep_alive": 0})

    def prewarm(self, model: str, keep_alive: str, *, num_ctx: int | None = None) -> None:
        """Fire-and-forget load (POST /api/generate, empty prompt).

        ``num_ctx`` pins the context window at load time so the resident model
        matches what the probe will request. Without it Ollama loads at the
        model's *default* context and the first run against that instance runs at
        the wrong num_ctx (or eats a mid-run reload) — the block-first
        contamination observed in the axis-1 sweep.
        """
        payload: dict[str, Any] = {"model": model, "keep_alive": keep_alive}
        if num_ctx is not None:
            payload["options"] = {"num_ctx": num_ctx}
        self._post("/api/generate", payload)


def _model_resident(running: set[str], model: str) -> bool:
    """Match a bare tag against /api/ps names (which may carry a :latest suffix)."""
    if model in running:
        return True
    base = model.split(":", 1)[0]
    return any(r == model or r.split(":", 1)[0] == base for r in running)


def maybe_evict(
    prev_model: str | None,
    next_model: str,
    control: OllamaControl,
    keep_alive: str,
    *,
    num_ctx: int | None = None,
    poll_cap: float = EVICTION_POLL_CAP_SECONDS,
    sleep_fn=time.sleep,
    now_fn=time.monotonic,
) -> bool:
    """Run the eviction protocol when the model changes; skip when it doesn't.

    On model change: (1) evict the outgoing model, (2) poll /api/ps until it's
    gone (``poll_cap`` cap → :class:`EvictionTimeout`), (3) pre-warm the incoming
    model at ``num_ctx`` and verify residency. Returns True if eviction ran,
    False if skipped.
    """
    if prev_model is not None and prev_model == next_model:
        return False  # same model stays resident — no eviction

    if prev_model is not None:
        control.evict(prev_model)
        deadline = now_fn() + poll_cap
        while _model_resident(control.list_running(), prev_model):
            if now_fn() >= deadline:
                raise EvictionTimeout(
                    f"{prev_model} still resident after {poll_cap}s"
                )
            sleep_fn(1.0)

    control.prewarm(next_model, keep_alive, num_ctx=num_ctx)
    # Verify residency (best-effort — pre-warm is async, give it the same window).
    deadline = now_fn() + poll_cap
    while not _model_resident(control.list_running(), next_model):
        if now_fn() >= deadline:
            raise EvictionTimeout(f"{next_model} did not become resident after {poll_cap}s")
        sleep_fn(1.0)
    return True


# ------------------------------------------------------------------ preflight


def preflight(
    campaign: Campaign,
    *,
    profiles: ProfileSet,
    control: OllamaControl | None = None,
    live: bool = True,
) -> None:
    """Fail-fast validation. Static checks always run; live checks only when
    ``live`` (skipped for --dry-run so it works offline)."""
    # Profiles resolve.
    for run in campaign.runs:
        profiles.select(run.profile)  # raises AgoraError on unknown name
    # Probe files exist + have a registered runner.
    for run in campaign.runs:
        if not (REPO_ROOT / run.probe).is_file():
            raise FileNotFoundError(f"probe file not found: {run.probe}")
        if run.probe not in PROBE_RUNNERS:
            raise ValueError(f"no runner registered for probe {run.probe!r}")
    if not live:
        return
    if control is None:
        control = OllamaControl()
    if not control.reachable():
        raise RuntimeError("Ollama daemon not reachable — is `ollama serve` running?")
    local = control.list_local()
    wanted = {model_for_profile(r.profile, profiles) for r in campaign.runs}
    missing = sorted(
        m for m in wanted if not _model_resident(local, m)
    )
    if missing:
        raise RuntimeError(
            f"models not present locally (pull them first): {missing}"
        )


# ------------------------------------------------------------------ run loop


def _synthetic_failure_record(
    run: dict[str, Any], campaign: Campaign, *, exit_code: int, notes: str
) -> dict[str, Any]:
    """A minimal schema-v1 RunRecord for a run whose child failed to emit one."""
    from agora.observe.jsonl import ArmSpec, ProfileSnapshot, RunRecord

    profiles = load_profiles()
    try:
        prof = profiles.select(run["profile"])
        snap = ProfileSnapshot(
            name=prof.name or run["profile"],
            model=prof.model,
            num_ctx=run["params"].get("num_ctx", prof.num_ctx),
            max_tokens=run["params"].get("max_tokens", prof.max_tokens),
            temperature=run["params"].get("temperature", prof.temperature),
            seed=run["params"].get("seed", prof.seed),
            keep_alive=prof.keep_alive,
        )
    except Exception:  # noqa: BLE001 — synthesize a minimal snapshot
        snap = ProfileSnapshot(model="unknown")
    rec = RunRecord(
        run_id=run["id"],
        started_at="",
        ended_at="",
        duration_s=0.0,
        probe_name=campaign.name,
        flow_path=run["probe"],
        project_name=campaign.name,
        profile=snap,
        arm=ArmSpec(**run["arm"]),
        success=False,
        exit_code=exit_code,
        tasks_total=0,
        tasks_passed=0,
        tasks_failed=0,
        tasks_first_pass=0,
        async_leak_hits=0,
        tokens_in=0,
        tokens_out=0,
        ollama_version="unknown",
        git_commit="unknown",
        host="",
        notes=notes[:2000],
    )
    return rec.model_dump()


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _popen_kwargs_detached() -> dict[str, Any]:
    """Start the child in its own process group so a SIGINT to the campaign
    does NOT propagate to the running child (we never kill it mid-run)."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def run_campaign(path: str | Path, *, dry_run: bool = False) -> int:
    campaign = load_campaign(path)
    profiles = load_profiles()
    control = OllamaControl()
    output_dir = Path(campaign.defaults.output_dir)

    preflight(campaign, profiles=profiles, control=control, live=not dry_run)

    plan = expand_plan(campaign)

    if dry_run:
        print(f"[dry-run] campaign {campaign.name}: {len(plan)} runs")
        for run in plan:
            arm = run["arm"]
            print(
                f"  {run['id']}: probe={run['probe']} profile={run['profile']} "
                f"arm={arm['scaffolding']}/{arm['strictness']} repeat={run['repeat']}"
            )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "plan.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in plan), encoding="utf-8"
    )

    done_ids = scan_done(output_dir) if campaign.defaults.resume else set()
    pending = resume_filter(plan, done_ids)
    print(
        f"[*] campaign {campaign.name}: {len(plan)} runs, "
        f"{len(done_ids)} already done, {len(pending)} pending"
    )

    interrupted = {"flag": False}

    def _on_sigint(_sig, _frame):
        interrupted["flag"] = True
        print("\n[*] SIGINT — will finish the current run then stop.", flush=True)

    signal.signal(signal.SIGINT, _on_sigint)

    camp_run_jsonl = output_dir / "run.jsonl"
    camp_tasks_jsonl = output_dir / "tasks.jsonl"
    prev_model: str | None = None

    for run in pending:
        if interrupted["flag"]:
            break
        next_model = model_for_profile(run["profile"], profiles)
        prof = profiles.select(run["profile"])
        keep_alive = prof.keep_alive
        # Pin the pre-warm context to the run's num_ctx (params override profile)
        # so the resident model matches what the probe requests — otherwise the
        # block-first run loads at the model default context.
        num_ctx = run.get("params", {}).get("num_ctx", prof.num_ctx)
        try:
            evicted = maybe_evict(prev_model, next_model, control, keep_alive, num_ctx=num_ctx)
            if evicted:
                print(f"[*] evicted → pre-warmed {next_model}")
        except EvictionTimeout as exc:
            print(f"[!] eviction timeout: {exc} — ABORTING campaign", flush=True)
            return 2

        run_dir = output_dir / run["id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_probe_command(run, run_dir)
        print(f"[*] {run['id']}: launching {run['profile']} ({next_model})", flush=True)
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), **_popen_kwargs_detached())
        exit_code = proc.wait()

        run_records = _read_jsonl(run_dir / "run.jsonl")
        if exit_code != 0 and not run_records:
            tail = ""
            log = run_dir / "run.log"
            if log.is_file():
                tail = log.read_text(encoding="utf-8", errors="replace")[-1000:]
            synth = _synthetic_failure_record(
                run, campaign, exit_code=exit_code,
                notes=f"probe exited {exit_code}; run.log tail:\n{tail}",
            )
            _append_jsonl(run_dir / "run.jsonl", [synth])
            run_records = [synth]
            print(f"[!] {run['id']}: probe exit {exit_code} — synthetic failure record", flush=True)

        _append_jsonl(camp_run_jsonl, run_records)
        _append_jsonl(camp_tasks_jsonl, _read_jsonl(run_dir / "tasks.jsonl"))
        prev_model = next_model

    if interrupted["flag"]:
        print(
            f"[*] stopped after SIGINT. Resume with: "
            f"python scripts/run_campaign.py {path}",
            flush=True,
        )
    else:
        print(f"[*] campaign {campaign.name} complete.")
    return 0


def main() -> int:
    # Status lines contain non-ASCII (→); Windows stdout is cp1252 by default,
    # and this script also runs as a subprocess of run_sweep_staged.py where the
    # child gets a fresh cp1252 stdout. Force UTF-8 before any print.
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run a sequential model-characterization campaign.")
    parser.add_argument("campaign", help="Path to the campaign YAML.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + print the expanded plan, then exit 0 without launching runs.",
    )
    args = parser.parse_args()
    return run_campaign(args.campaign, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
