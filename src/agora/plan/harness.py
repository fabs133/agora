"""Reusable orchestrator harness for plan runners.

Every runner script in ``scripts/`` opens a Matrix client, logs in, optionally
invites an observer user to auto-created rooms, builds an ``Orchestrator``,
runs a project, and prints a summary. That boilerplate is ~80 lines of mostly
identical setup. This module factors it out so a plan runner (and the
plan-builder runner) don't have to duplicate it.

The harness does not hold any CLI-parsing or per-project task authoring —
callers pass the ``(agents, tasks, staged_tasks)`` triple that
:func:`agora.plan.loader.instantiate_plan` produces.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agora.core.agent import AgentConfig
from agora.core.task import Task
from agora.fleet.llm_adapter import create_llm_adapter
from agora.fleet.orchestrator import Orchestrator, ProjectResult
from agora.fleet.vram import check_model_fits, raise_if_wont_fit
from agora.matrix.client import AgoraMatrixClient
from agora.matrix.room_manager import RoomManager


@dataclass
class HarnessConfig:
    """Everything a plan runner needs to stand up an Orchestrator.

    Defaults mirror the env knobs used by the hand-written runner scripts
    (``AGORA_MATRIX_HOMESERVER``, ``AGORA_OLLAMA_BASE_URL`` etc.) so callers
    who want the same behaviour can just ``HarnessConfig.from_env()``.
    """

    homeserver: str = "http://localhost:6167"
    server_name: str = "agora.local"
    system_user: str = "@agora:agora.local"
    system_password: str = "agora-dev-pass"
    observer_user: str = "@fabs:agora.local"
    ollama_base_url: str = "http://localhost:11434"
    review_timeout_seconds: float = 300.0
    max_parallel_agents: int = 2
    max_task_retries: int = 2
    work_dir: Path = field(default_factory=lambda: Path("workspace"))
    knowledge_cache_dir: Path | None = None
    enable_observer: bool = True
    enable_web_fetch: bool = True
    fetch_max_text_bytes: int = 65_536
    auto_hooks_enabled: bool = True
    # Opt-in for plan-authoring tools (plan_upsert_agent, plan_add_task_spec,
    # plan_finalize). The plan-builder runner sets this True; run_plan.py and
    # other executors leave it False so emitted plans don't expose the
    # meta-authoring surface to their agents.
    plan_authoring_enabled: bool = False
    # v2.5: how many times the router may re-dispatch an upstream task because
    # a downstream test failed with an ImportError pointing at one of its
    # files. Separate from ``max_task_retries`` so routed retries don't
    # compete for the owning task's normal retry pool.
    routed_retry_budget: int = 2

    @classmethod
    def from_env(cls, work_dir: str | Path = "workspace") -> "HarnessConfig":
        """Pull the same env knobs the existing runners read."""
        work_dir_path = Path(work_dir)
        return cls(
            homeserver=os.getenv("AGORA_MATRIX_HOMESERVER", "http://localhost:6167"),
            system_password=os.getenv("AGORA_MATRIX_PASSWORD", "agora-dev-pass"),
            observer_user=os.getenv("AGORA_OBSERVER_USER", "@fabs:agora.local"),
            ollama_base_url=os.getenv("AGORA_OLLAMA_BASE_URL", "http://localhost:11434"),
            review_timeout_seconds=float(
                os.getenv("AGORA_REVIEW_TIMEOUT_SECONDS", "300")
            ),
            max_parallel_agents=int(os.getenv("AGORA_MAX_PARALLEL_AGENTS", "2")),
            max_task_retries=int(os.getenv("AGORA_MAX_TASK_RETRIES", "2")),
            routed_retry_budget=int(os.getenv("AGORA_ROUTED_RETRY_BUDGET", "2")),
            work_dir=work_dir_path,
            knowledge_cache_dir=work_dir_path / ".knowledge",
        )


async def preflight_vram(model: str, base_url: str) -> None:
    """Best-effort VRAM check + warm-up gate. Prints the reason line, raises on hard fail.

    Skipped for non-Ollama models (API-backed providers like openai/*,
    anthropic/*, gemini/*, claude-*, claude-code/*) — there's no local GPU
    memory to budget against.
    """
    if not model.startswith("ollama/"):
        print(f"[*] VRAM check skipped for {model} (remote provider)")
        return
    print(f"[*] VRAM check for {model}...")
    check = await check_model_fits(model, base_url=base_url)
    print(f"  {check.reason}")
    raise_if_wont_fit(check, model)


async def build_matrix_client(cfg: HarnessConfig) -> AgoraMatrixClient:
    """Log in as the system user and wire the auto-invite shim if observer is set."""
    print(f"[*] Logging into Conduit as {cfg.system_user}")
    client = AgoraMatrixClient(homeserver=cfg.homeserver, user_id=cfg.system_user)
    await client.login(cfg.system_password)

    if cfg.observer_user:
        _orig_create_room = client.create_room

        async def _create_with_observer(
            name, topic="", invite=None, initial_state=None
        ):
            merged = list(invite or [])
            if cfg.observer_user not in merged:
                merged.append(cfg.observer_user)
            return await _orig_create_room(
                name=name, topic=topic, invite=merged, initial_state=initial_state
            )

        client.create_room = _create_with_observer  # type: ignore[assignment]
        print(f"[*] Auto-inviting {cfg.observer_user} to every created room")

    return client


def build_orchestrator(
    cfg: HarnessConfig,
    client: AgoraMatrixClient,
    model: str,
) -> Orchestrator:
    """Construct an :class:`Orchestrator` with sensible defaults for plan runners."""
    room_manager = RoomManager(client, homeserver_name=cfg.server_name)

    def llm_factory(model_ref: str):
        # Empty model → fall back to the harness default. v2.3 plan-builder
        # emits agents with ``model=""`` (the model can't reliably guess a
        # valid model id); the harness owns the runtime model choice anyway.
        if not model_ref:
            model_ref = model
        # Build adapter-specific kwargs. Only Ollama needs our base_url;
        # LiteLLM providers pick up auth from env vars; Anthropic direct
        # reads ANTHROPIC_API_KEY when present (kept for back-compat with
        # scripts that set it explicitly).
        kwargs: dict[str, Any] = {"timeout_seconds": 600.0}
        if model_ref.startswith("ollama/"):
            kwargs["base_url"] = cfg.ollama_base_url
        if model_ref.startswith("claude-") and not model_ref.startswith("claude-code/"):
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                kwargs["api_key"] = api_key
        return create_llm_adapter(model_ref, **kwargs)

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    if cfg.knowledge_cache_dir is not None:
        cfg.knowledge_cache_dir.mkdir(parents=True, exist_ok=True)

    return Orchestrator(
        matrix_client=client,
        room_manager=room_manager,
        llm_factory=llm_factory,
        work_dir=str(cfg.work_dir),
        homeserver_name=cfg.server_name,
        max_parallel_agents=cfg.max_parallel_agents,
        enable_observer=cfg.enable_observer,
        repo_root=str(cfg.work_dir),
        knowledge_cache_dir=(
            str(cfg.knowledge_cache_dir) if cfg.knowledge_cache_dir else None
        ),
        ollama_base_url=cfg.ollama_base_url,
        skip_warmup=False,
        warmup_deadline=600.0,
        review_timeout_seconds=cfg.review_timeout_seconds,
        enable_web_fetch=cfg.enable_web_fetch,
        fetch_timeout_seconds=30.0,
        fetch_max_bytes=1_048_576,
        fetch_max_text_bytes=cfg.fetch_max_text_bytes,
        auto_hooks_enabled=cfg.auto_hooks_enabled,
        plan_authoring_enabled=cfg.plan_authoring_enabled,
        routed_retry_budget=cfg.routed_retry_budget,
    )


def seed_workspace(flow: Any, project_name: str, work_dir_root: Path) -> None:
    """Write framework-owned shared files into the executor's project work_dir.

    v2.6+ plans embed free-form context (``brief``, ``api_spec``) directly in
    the YAML. The executor runs in a fresh per-project work_dir that doesn't
    have those files yet — scaffolders + LLM stages expect them present at
    well-known paths (``plan/brief.md``, ``plan/api_spec.md``). This helper
    seeds them ONCE at kickoff from ``flow.brief`` / ``flow.api_spec``.

    v2.7: also pre-writes ``src/*.py`` STUB files from ``api_spec``. Each
    stub has the matching class/function signatures + ``raise
    NotImplementedError`` bodies. This means:

      - ``py_compiles`` + ``file_exists`` postconditions on src/ files pass
        trivially at kickoff (the stub is valid python).
      - Contract tests can use REAL module-level imports (``from src.foo
        import Bar``) because the file exists — no more "deferred imports"
        needed. Collection never fails on ``ImportError``.
      - The implementer's job collapses to "replace NotImplementedError
        with real body" via the existing ``add_class_method`` /
        ``add_function`` upsert-by-name tools (Sprint 7.4).
      - Tester + implementer physically CANNOT disagree on API — the stub
        file is the single framework-owned source of truth.

    Safe to call even when both fields are empty (no-op). Never overwrites
    an existing file — if the executor workspace is being re-used (rerun
    after partial failure), the on-disk copy wins.
    """
    from agora.plan.api_spec import parse_api_spec, render_impl_stub

    project_dir = work_dir_root / project_name
    plan_dir = project_dir / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    brief = getattr(flow, "brief", "") or ""
    if brief:
        brief_path = plan_dir / "brief.md"
        if not brief_path.exists():
            brief_path.write_text(brief, encoding="utf-8")
    api_spec_text = getattr(flow, "api_spec", "") or ""
    if not api_spec_text:
        return
    spec_path = plan_dir / "api_spec.md"
    if not spec_path.exists():
        spec_path.write_text(api_spec_text, encoding="utf-8")

    modules = parse_api_spec(api_spec_text)
    for module in modules:
        stub_rel = module.path.replace("\\", "/")
        # v2.7(c): only stub PRODUCTION modules. If the spec wrongly
        # included a test file path (``tests/test_X.py`` or
        # ``src/tests/X.py``), skip it — tests get scaffolded separately
        # by the test-authoring pipeline (Sprint 7.2-7.3).
        if stub_rel.startswith("tests/") or stub_rel.startswith("src/tests/"):
            continue
        if not stub_rel.startswith("src/") or not stub_rel.endswith(".py"):
            continue
        stub_path = project_dir / stub_rel
        if stub_path.exists() and stub_path.stat().st_size > 0:
            continue  # respect prior run's partial state
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(render_impl_stub(module), encoding="utf-8")
    # Also drop an `__init__.py` into the common src/ package root so the
    # module path ``src.foo`` resolves without the caller wiring it.
    src_root = project_dir / "src"
    if src_root.is_dir():
        init_path = src_root / "__init__.py"
        if not init_path.exists():
            init_path.write_text("", encoding="utf-8")


async def run_plan_project(
    cfg: HarnessConfig,
    project_name: str,
    agents: list[AgentConfig],
    tasks: list[Task],
    staged_tasks: dict[str, Any] | None = None,
    model_hint: str | None = None,
    max_loopbacks: int = 2,
) -> ProjectResult:
    """End-to-end: preflight → login → orchestrate → run → close client.

    ``model_hint`` is used for the VRAM preflight; falls back to the first
    agent's model (runners usually use a single model). Returns the
    :class:`ProjectResult` and prints a one-line summary per task.
    """
    model = model_hint or (agents[0].model if agents else "ollama/qwen2.5:7b-instruct")
    await preflight_vram(model, cfg.ollama_base_url)

    client = await build_matrix_client(cfg)
    try:
        orchestrator = build_orchestrator(cfg, client, model)
        print(f"[*] Running project '{project_name}' (observer={cfg.enable_observer})")
        print(f"   review_timeout_seconds={cfg.review_timeout_seconds}")
        print(f"   max_task_retries={cfg.max_task_retries}")
        print()
        result = await orchestrator.run_project(
            project_name,
            agents,
            tasks,
            max_loopbacks=max_loopbacks,
            staged_tasks=staged_tasks,
            max_task_retries=cfg.max_task_retries,
        )
    finally:
        await client.close()

    _print_summary(result)
    return result


def _print_summary(result: ProjectResult) -> None:
    print("\n" + "=" * 72)
    print(f"Project phase: {result.project.phase.value}")
    print(f"Success: {result.success}")
    print(f"Project room: {result.project_room_id}")
    print(f"Duration: {result.duration_seconds:.1f}s")
    print(
        f"Tokens: in={int(result.total_tokens.get('input_tokens', 0))}, "
        f"out={int(result.total_tokens.get('output_tokens', 0))}"
    )
    # Per-run USD cost surfaces only for LiteLLM-backed runs; Ollama +
    # direct Anthropic adapters don't populate cost_usd.
    cost = result.total_tokens.get("cost_usd", 0.0)
    if cost:
        # Sub-cent values still useful for cross-provider comparison.
        print(f"Cost:   ${float(cost):.4f} USD")
    for r in result.task_results:
        mark = "OK" if r.success else "FAIL"
        print(f"  [{mark}] {r.task_id}: {r.iterations} iter  -> {r.output[:80]}")


def force_utf8_stdio() -> None:
    """Windows cp1252 cannot encode many characters LLMs produce. Force UTF-8.

    Call from a runner's top-level before any logging is configured.
    """
    import io

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )


__all__ = [
    "HarnessConfig",
    "build_matrix_client",
    "build_orchestrator",
    "force_utf8_stdio",
    "preflight_vram",
    "run_plan_project",
]
