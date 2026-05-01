"""Generic plan runner: load a v2.0 plan YAML, run it through the Orchestrator.

    .venv/Scripts/python.exe scripts/run_plan.py flows/fastapi-crud.plan.yaml
    .venv/Scripts/python.exe scripts/run_plan.py flows/fastapi-crud.plan.yaml \\
        --project-name fastapi-crud-rerun --var foo=bar

Environment knobs are inherited from :class:`agora.plan.harness.HarnessConfig`
(``AGORA_MATRIX_HOMESERVER``, ``AGORA_OLLAMA_BASE_URL``, ``AGORA_LLM_MODEL``,
``AGORA_MAX_PARALLEL_AGENTS``, ``AGORA_MAX_TASK_RETRIES``, etc.).

``--var key=value`` injects into the YAML's ``${variable}`` substitution env;
``--project-name`` sets both the Agora project name and is also available as
``${project_name}`` in the YAML.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.plan.harness import (
    HarnessConfig,
    force_utf8_stdio,
    run_plan_project,
    seed_workspace,
)
from agora.plan.loader import instantiate_plan, load_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agora generic plan runner")
    parser.add_argument("plan", type=Path, help="path to a v2.0 plan YAML")
    parser.add_argument(
        "--project-name",
        default=None,
        help="project name (defaults to the plan's own name)",
    )
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="inject a ${variable} substitution; may repeat",
    )
    return parser.parse_args()


def _split_vars(kv_pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in kv_pairs:
        if "=" not in pair:
            raise SystemExit(f"--var must be KEY=VALUE, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v
    return out


async def main() -> None:
    force_utf8_stdio()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    logging.getLogger("nio").setLevel(logging.WARNING)

    args = parse_args()
    if not args.plan.is_file():
        print(f"[!] plan not found: {args.plan}", file=sys.stderr)
        sys.exit(2)

    plan = load_plan(args.plan)
    project_name = args.project_name or plan.name

    # Default model comes from env; YAML can reference ${model} to pick it up.
    model = os.getenv("AGORA_LLM_MODEL", "ollama/qwen2.5:7b-instruct")
    vars_from_cli = _split_vars(args.var)
    variables: dict[str, str] = {"model": model}
    variables.update(vars_from_cli)

    agents, tasks, staged_tasks = instantiate_plan(
        plan, project_name=project_name, variables=variables
    )

    cfg = HarnessConfig.from_env(work_dir=REPO_ROOT / "workspace")

    # v2.7: seed the executor workspace with plan-level shared artifacts
    # (brief.md, api_spec.md) BEFORE any task runs. This is how the tester
    # and implementer agents see the same framework-owned API surface —
    # without it they each re-guess, and their guesses don't match.
    seed_workspace(plan.flow, project_name, cfg.work_dir)

    print(f"[*] plan: {args.plan}")
    print(f"[*] project: {project_name}  tasks={len(tasks)}  staged={len(staged_tasks)}")
    print(f"[*] model: {model}")
    print()

    await run_plan_project(
        cfg,
        project_name=project_name,
        agents=agents,
        tasks=tasks,
        staged_tasks=staged_tasks,
        model_hint=model,
    )


if __name__ == "__main__":
    asyncio.run(main())
