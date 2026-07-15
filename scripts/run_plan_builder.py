"""Interactive plan-builder runner.

Usage:

    .venv/Scripts/python.exe scripts/run_plan_builder.py \\
        --goal "Build a command-line RSS reader with subscribe, list, fetch"

The runner pre-populates a few scaffold files under ``workspace/plan-builder/``
so the planner tasks can fill them in via ``edit_file_replace`` (rather than
authoring YAML from scratch — 7B can't). Then it hands off to the generic
plan runner via ``agora.plan.harness.run_plan_project``.

What the planner actually produces for MVP:
    plan/brief.md         — human-readable intent summary
    plan/decisions.yaml   — the user's answers to 2 blocking design questions
    plan/tasks.md         — 4-8 task id + descriptions (not a runnable plan yet)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.plan.harness import HarnessConfig, force_utf8_stdio, run_plan_project
from agora.plan.loader import instantiate_plan, load_plan
from agora.plan.predicate_registry import (
    describe_registered_predicates,
)

PLAN_YAML = REPO_ROOT / "flows" / "plan-builder.plan.yaml"
PROJECT_NAME = "plan-builder"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agora interactive plan-builder")
    parser.add_argument(
        "--goal",
        required=True,
        help="Free-text description of what the user wants to build.",
    )
    parser.add_argument(
        "--project-name",
        default=PROJECT_NAME,
        help=f"Project name (defaults to {PROJECT_NAME!r})",
    )
    return parser.parse_args()


_LIBRARY_DOCS_URLS: dict[str, str] = {
    "stdlib-only": "https://docs.python.org/3/library/argparse.html",
    "click": "https://click.palletsprojects.com/en/stable/",
    "fastapi": "https://fastapi.tiangolo.com/tutorial/",
    "flask": "https://flask.palletsprojects.com/en/stable/quickstart/",
}


def _render_brief_skeleton(project_name: str, goal: str) -> str:
    """Best-effort structured brief from the raw --goal text.

    Splits the goal into an intro sentence (kept as the summary paragraph) +
    subsequent sentences (converted to ``- <bullet>`` under ``## Key
    deliverables``). Sentence splitting is comma/semicolon/period-aware but
    deliberately simple — the goal is STRUCTURAL SATISFACTION of downstream
    postconditions (``file_contains(plan/brief.md, 'Key deliverables')``),
    not prose polish. The LLM's gather_context step can still edit this file.
    """
    import re

    text = goal.strip()
    pretty_name = project_name.replace("-", " ").replace("_", " ").title()
    # Split on sentence terminators. Keep the first sentence as the summary;
    # subsequent sentences become bullets (further split on semicolon/", ").
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not sentences:
        sentences = [text or f"Build {pretty_name}."]
    summary = sentences[0].rstrip(".") + "."
    bullets: list[str] = []
    for s in sentences[1:]:
        # Secondary split on semicolons + "; " to break compound sentences
        # into individual deliverables.
        for chunk in re.split(r"\s*;\s*", s):
            chunk = chunk.strip().rstrip(".")
            if chunk and len(chunk.split()) >= 2:
                bullets.append(chunk)
    if not bullets:
        bullets = [summary.rstrip(".")]
    lines = [
        f"# Brief: {pretty_name}",
        "",
        summary,
        "",
        "## Key deliverables",
    ]
    lines.extend(f"- {b}" for b in bullets[:8])
    lines.append("")
    return "\n".join(lines)


def scaffold_workspace(project_name: str, goal: str) -> Path:
    """Lay down kb/ + placeholder files the planner tasks expect.

    Happens BEFORE the orchestrator starts. Writes:

    - ``plan/kb/goal.md`` — user's raw goal.
    - ``plan/kb/postcondition_catalog.md`` — typed catalog of every registered
      predicate (name + arg schema) so the LLM knows exactly what arguments
      to pass to ``plan_attach_postcondition``.
    - ``plan/kb/library_urls.md`` — canonical docs URL per library answer_id;
      the ``research_library`` stage looks up the chosen library and uses
      ``fetch_url`` to pull real doc content.
    """
    work_dir = REPO_ROOT / "workspace" / project_name
    plan_dir = work_dir / "plan"
    kb_dir = plan_dir / "kb"
    for p in (work_dir, plan_dir, kb_dir):
        p.mkdir(parents=True, exist_ok=True)

    (kb_dir / "goal.md").write_text(
        f"# Project goal\n\n{goal.strip()}\n", encoding="utf-8"
    )

    # Pre-scaffold plan/brief.md so gather_context becomes a read-and-confirm
    # step instead of write-from-scratch. 7B reliably misses structural section
    # headers (observed: wrote '# Brief:' + summary, skipped '## Key
    # deliverables') and then returns empty content on retries. The brief's
    # content is functionally derivable from the goal text — so emit it
    # deterministically and let the LLM just acknowledge.
    brief_path = plan_dir / "brief.md"
    if not brief_path.exists():
        brief_path.write_text(_render_brief_skeleton(project_name, goal), encoding="utf-8")

    # Catalog of registered predicates — schema-rich so the LLM can call
    # ``plan_attach_postcondition`` without guessing argument names.
    catalog_lines: list[str] = [
        "# Available postconditions",
        "",
        "Each entry below is a predicate name you can pass to",
        "`plan_attach_postcondition(task_id, name, args={...})`. The `args`",
        "object must match the argument names shown — required fields have no",
        "default.",
        "",
    ]
    for entry in describe_registered_predicates():
        arg_descs = []
        for a in entry["args"]:
            piece = f"{a['name']}: {a['type']}"
            if not a["required"]:
                piece += f" = {a.get('default')!r}"
            arg_descs.append(piece)
        catalog_lines.append(
            f"- `{entry['name']}({', '.join(arg_descs)})`"
        )
    catalog_lines.append("")
    (kb_dir / "postcondition_catalog.md").write_text(
        "\n".join(catalog_lines), encoding="utf-8"
    )

    # Library docs URL map — each decision answer_id maps to a real
    # documentation URL the ``research_library`` stage fetches.
    url_lines = ["# Library documentation URLs", ""]
    for answer_id, url in _LIBRARY_DOCS_URLS.items():
        url_lines.append(f"- `{answer_id}`: {url}")
    url_lines.append("")
    (kb_dir / "library_urls.md").write_text(
        "\n".join(url_lines), encoding="utf-8"
    )

    return work_dir


async def main() -> None:
    force_utf8_stdio()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    logging.getLogger("nio").setLevel(logging.WARNING)

    args = parse_args()
    work_dir = scaffold_workspace(args.project_name, args.goal)
    print(f"[*] goal: {args.goal}")
    print(f"[*] workspace: {work_dir}")
    print("[*] scaffold files: plan/kb/goal.md, plan/kb/postcondition_catalog.md, plan/decisions.yaml")
    print()

    plan = load_plan(PLAN_YAML)

    from agora.config import get_settings

    settings = get_settings()
    # Default model comes from Settings (env is read only in config.py).
    model = settings.llm_model
    agents, tasks, staged_tasks = instantiate_plan(
        plan, project_name=args.project_name, variables={"model": model}
    )

    cfg = HarnessConfig.from_settings(settings, work_dir=REPO_ROOT / "workspace")
    # Plan-builder authors plans; opts in to the plan_authoring tool category
    # (plan_upsert_agent, plan_add_task_spec, plan_finalize). Emitted plans
    # run via scripts/run_plan.py which leaves this False, so executors never
    # see the meta-authoring tools.
    cfg.plan_authoring_enabled = True
    print(f"[*] plan: {PLAN_YAML.relative_to(REPO_ROOT)}  tasks={len(tasks)}  staged={len(staged_tasks)}")
    print(f"[*] model: {model}")
    print(f"[*] open Element as {cfg.observer_user} to see the polls + answer them")
    print()

    await run_plan_project(
        cfg,
        project_name=args.project_name,
        agents=agents,
        tasks=tasks,
        staged_tasks=staged_tasks,
        model_hint=model,
        max_loopbacks=1,
    )


if __name__ == "__main__":
    asyncio.run(main())
