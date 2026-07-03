"""Run the axis-1 tool-call fidelity probe against the live Conduit + Ollama stack.

Seeds the four ``plan/*.txt`` fixtures into the project work_dir, then runs
``flows/tool-call-fidelity.plan.yaml`` through ``build_orchestrator`` with the
JSONL observer enabled. The probe is an atomic tool-call measurement, so
auto-hooks are turned OFF: mark_complete must be the model's own call (not a
framework-synthesized one) for the ``mark_complete_called`` postcondition to
mean anything, and tool-call counts reflect only model-emitted calls.

Usage:

    AGORA_PROFILE=qwen-coder-7b .venv/Scripts/python.exe scripts/run_tool_call_fidelity.py

Config mirrors scripts/run_discord_bot_test.py: AGORA_PROFILE selects the
model profile; per-field env overrides (AGORA_LLM_TEMPERATURE, AGORA_LLM_SEED,
AGORA_LLM_NUM_CTX, AGORA_LLM_MAX_TOKENS) layer on top. AGORA_RUN_OUTPUT_DIR
controls where run.jsonl / tasks.jsonl land (default runs_out/_default/<run_id>/).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from pathlib import Path

# Repo-relative imports without installing as a package.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.fleet.profiles import apply_env_overrides, build_llm_factory, load_profiles
from agora.fleet.strategies import StrategyAdapter, resolve
from agora.observe.jsonl import (
    ArmSpec,
    RunObserver,
    git_commit_short,
    profile_snapshot_from,
    query_ollama_version,
)
from agora.plan.harness import (
    HarnessConfig,
    build_matrix_client,
    build_orchestrator,
    force_utf8_stdio,
    preflight_vram,
)
from agora.plan.loader import instantiate_plan, load_plan

force_utf8_stdio()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logging.getLogger("nio").setLevel(logging.WARNING)

FLOW_PATH = "flows/tool-call-fidelity.plan.yaml"
PROJECT_NAME = "tool-call-fidelity"

#: The four seed fixtures. Fixed content so byte-exact postconditions are
#: deterministic across every model and repeat in the campaign.
SEED_FILES: dict[str, str] = {
    "plan/seed.txt": (
        "alpha line one\n"
        "beta line two\n"
        "gamma line three\n"
        "delta line four\n"
    ),
    "plan/seed_a.txt": (
        "apple\n"
        "apricot\n"
        "avocado\n"
    ),
    "plan/seed_b.txt": (
        "blueberry\n"
        "blackberry\n"
        "boysenberry\n"
    ),
    "plan/redirect.txt": "Read the file at plan/redirect_target.txt next.",
    "plan/redirect_target.txt": (
        "target line one\n"
        "target line two\n"
        "target line three\n"
    ),
}


def seed_probe_files(work_dir: Path, project_name: str) -> Path:
    """Write the four plan/*.txt fixtures into the project's work_dir. Idempotent."""
    project_dir = work_dir / project_name
    for rel, content in SEED_FILES.items():
        path = project_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return project_dir


async def main() -> None:
    import os

    profile = apply_env_overrides(load_profiles().select(os.getenv("AGORA_PROFILE", "")))
    print(
        f"[*] Profile: {profile.name or '<unnamed>'} → model={profile.model}, "
        f"num_ctx={profile.num_ctx}, temp={profile.temperature}, seed={profile.seed}"
    )

    cfg = HarnessConfig.from_env()
    # Atomic tool-call probe: no framework auto-hooks (no synthesized
    # mark_complete, no auto git_commit) so the signal is the model's own calls.
    cfg.auto_hooks_enabled = False

    project_dir = seed_probe_files(cfg.work_dir, PROJECT_NAME)
    print(f"[*] Seeded {len(SEED_FILES)} fixtures under {project_dir / 'plan'}")

    await preflight_vram(
        profile.model,
        profile.ollama.base_url,
        safety_margin_mib=profile.vram.safety_margin_mib,
    )

    plan = load_plan(FLOW_PATH)
    agents, tasks, staged = instantiate_plan(plan, PROJECT_NAME)

    run_id = uuid.uuid4().hex
    output_dir = RunObserver.resolve_output_dir(run_id)
    # Arm is set by the campaign harness via env; a standalone run defaults to
    # rich/strict (the ArmSpec default) so direct invocation is unchanged.
    arm = ArmSpec(
        scaffolding=os.getenv("AGORA_ARM_SCAFFOLDING", "rich"),
        strictness=os.getenv("AGORA_ARM_STRICTNESS", "strict"),
    )
    # Per-model prompting strategy (axis-1 v2). Unset ⇒ control cell: strategy
    # is None and no wrapper is constructed (build_orchestrator builds the bare
    # factory), byte-identical to v1.
    strategy_name = os.getenv("AGORA_STRATEGY", "").strip() or None
    strategy = resolve(strategy_name)
    if strategy is not None:
        print(f"[*] Strategy: {strategy_name}")
    observer = RunObserver(
        run_id=run_id,
        output_dir=output_dir,
        probe_name=plan.flow.name,
        flow_path=FLOW_PATH,
        project_name=PROJECT_NAME,
        profile=profile_snapshot_from(profile),
        arm=arm,
        ollama_version=query_ollama_version(profile.ollama.base_url),
        git_commit=git_commit_short(REPO_ROOT),
        log_path=output_dir / "run.log",
        strategy=strategy_name,
        # v3 provenance: the harness config actually in force + the probe design
        # version carried from the flow file.
        harness={"tool_errors": cfg.tool_errors, "nudge_budget": cfg.nudge_budget},
        probe_version=getattr(plan.flow, "probe_version", None),
    )
    if cfg.tool_errors != "raw" or cfg.nudge_budget:
        print(f"[*] Harness: tool_errors={cfg.tool_errors} nudge_budget={cfg.nudge_budget}")
    print(f"[*] Run observer → {output_dir} (run_id={run_id})")

    # When a strategy is set, wrap the profile's factory so every adapter the
    # orchestrator builds is strategy-aware. None ⇒ pass nothing; the default
    # factory path in build_orchestrator is untouched.
    llm_factory = None
    if strategy is not None:
        _base_factory = build_llm_factory(profile)

        def llm_factory(model_ref: str, _f=_base_factory, _s=strategy):
            return StrategyAdapter(_f(model_ref), _s)

    client = await build_matrix_client(cfg)
    try:
        orchestrator = build_orchestrator(
            cfg,
            client,
            profile.model,
            profile=profile,
            observer=observer,
            llm_factory=llm_factory,
        )
        print(f"[*] Running probe '{PROJECT_NAME}' ({len(tasks)} tasks)")
        result = await orchestrator.run_project(
            PROJECT_NAME,
            agents,
            tasks,
            max_loopbacks=0,
            staged_tasks=staged,
        )
    finally:
        await client.close()

    print("\n" + "=" * 72)
    print(f"Success: {result.success}  Duration: {result.duration_seconds:.1f}s")
    for r in result.task_results:
        mark = "OK" if r.success else "FAIL"
        print(
            f"  [{mark}] {r.task_id}: {r.iterations} iter, "
            f"tool_calls={r.tool_calls_total} "
            f"(structured={r.tool_calls_structured}, "
            f"text_fallback={r.tool_calls_text_fallback})"
        )
    print(f"[*] JSONL: {output_dir / 'run.jsonl'}, {output_dir / 'tasks.jsonl'}")


if __name__ == "__main__":
    asyncio.run(main())
