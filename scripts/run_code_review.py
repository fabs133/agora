"""Code-review runner: Agora reviews a completed workspace and emits findings.

Given a directory of Python source (e.g. ``workspace/discord-bot-full/``),
this runner copies the ``.py`` files into a fresh review workspace, runs a
parallel per-file review DAG through Agora, and finally aggregates the
findings into ``review/REPORT.md``.

Output schema for each per-file review:

    # Review: <filename>

    - [SEVERITY] <file:line> | <category> | <description> | <suggested fix>
    - [SEVERITY] ...

The pipe-separated one-line format is machine-parseable (split on ``|``) and
easy for a 7B model to emit under a literal-template stage.

Run with:

    .venv/Scripts/python.exe scripts/run_code_review.py
    .venv/Scripts/python.exe scripts/run_code_review.py --target workspace/fastapi-crud/

Environment knobs:

    AGORA_MAX_PARALLEL_AGENTS=3
    AGORA_MAX_TASK_RETRIES=2
    AGORA_LLM_MODEL=ollama/qwen2.5:7b-instruct
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logging.getLogger("nio").setLevel(logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.llm_adapter import create_llm_adapter
from agora.fleet.orchestrator import Orchestrator
from agora.fleet.vram import check_model_fits, raise_if_wont_fit
from agora.matrix.client import AgoraMatrixClient
from agora.matrix.room_manager import RoomManager


HOMESERVER = os.getenv("AGORA_MATRIX_HOMESERVER", "http://localhost:6167")
SERVER_NAME = "agora.local"
SYSTEM_USER = "@agora:agora.local"
SYSTEM_PASSWORD = os.getenv("AGORA_MATRIX_PASSWORD", "agora-dev-pass")
OBSERVER_USER = os.getenv("AGORA_OBSERVER_USER", "@fabs:agora.local")
OLLAMA_BASE_URL = os.getenv("AGORA_OLLAMA_BASE_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("AGORA_LLM_MODEL", "ollama/qwen2.5:7b-instruct")
REVIEW_TIMEOUT = float(os.getenv("AGORA_REVIEW_TIMEOUT_SECONDS", "300"))
MAX_PARALLEL = int(os.getenv("AGORA_MAX_PARALLEL_AGENTS", "3"))
MAX_TASK_RETRIES = int(os.getenv("AGORA_MAX_TASK_RETRIES", "2"))
WORK_DIR = REPO_ROOT / "workspace"


# ---------------------------------------------------------------------- postconds

def _require(name: str, check):
    return make_predicate(name, name, check)


def _postcond_file_exists(rel: str):
    def check(ctx):
        artifacts = ctx.get("artifacts") or []
        return (
            any(rel in a for a in artifacts),
            f"expected an artifact containing {rel!r}",
        )

    return _require(f"artifact_contains_{rel.replace('/', '_')}"[:60], check)


def _postcond_mark_complete():
    def check(ctx):
        return (bool(ctx.get("completions")), "mark_complete was not called")

    return _require("mark_complete_called", check)


def _postcond_review_schema(rel: str):
    """The review file must have the `# Review: <name>` header AND at least
    one issue line starting with `- [` (the severity-bracketed schema).

    Catches the common failure of the model emitting prose instead of schema.
    """
    import re

    _issue_re = re.compile(r"^\s*-\s*\[(ERROR|WARN|INFO)\]", re.MULTILINE)

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        body = path.read_text(encoding="utf-8", errors="replace")
        if "# Review:" not in body:
            return (False, f"{rel} missing '# Review:' header")
        issues = _issue_re.findall(body)
        if not issues:
            return (False, f"{rel} contains no schema-formatted issue lines")
        return (True, "")

    return _require(f"{rel.replace('/', '_').replace('.', '_')}_has_schema"[:60], check)


# ---------------------------------------------------------------------- prompts

REVIEWER_INSTRUCTIONS = """\
You are the REVIEWER. For each task, read the provided source file and write
a structured review in the exact schema specified. Focus on real issues a
human reviewer would flag:

- Correctness: bugs, typos, shadowing, dead code, wrong imports
- Idioms: non-idiomatic Python (old-style formatting, missing type hints
  where all siblings have them, class structure)
- Testing: weak assertions, missing coverage of obvious edge cases
- Style only when it materially affects readability

Do NOT invent issues. If a file has only 1 issue, emit 1 issue. If a file is
clean, emit a single `- [INFO] <file>:0 | clean | No material issues | —` line.
"""


def _step(step: str, inputs: str, output: str, requirement: str, tools: str) -> str:
    return (
        f"STEP: {step}\n"
        f"INPUTS: {inputs}\n"
        f"OUTPUT: {output}\n"
        f"REQUIREMENT: {requirement}\n"
        f"TOOLS: {tools}\n"
        f"Always call mark_complete with artifacts=[\"{output}\"] when done."
    )


def build_agents() -> list[AgentConfig]:
    return [
        AgentConfig(
            name="reviewer",
            role=AgentRole.ARCHITECT,
            model=LLM_MODEL,
            instructions=REVIEWER_INSTRUCTIONS,
        ),
    ]


def _task(
    tid: str,
    agent: str,
    description: str,
    postconditions: tuple,
    depends_on: tuple[str, ...] = (),
    output_path: str = "",
) -> Task:
    return Task(
        id=tid,
        spec=Specification(
            postconditions=postconditions,
            description=f"{tid}: narrow single-action review step",
        ),
        description=description,
        agent_id=agent,
        depends_on=depends_on,
        status=TaskStatus.PENDING,
        output_path=output_path,
    )


def _review_task(source_name: str) -> Task:
    """One review task per source file. ``source_name`` like ``bot.py``."""
    review_name = f"review_{source_name.replace('.', '_')}"
    out_path = f"review/{source_name}.md"
    return _task(
        review_name,
        "reviewer",
        _step(
            step=f"Review {source_name} and write a structured findings report.",
            inputs=f"{source_name} (pre-loaded in the user message).",
            output=out_path,
            requirement=f"{out_path} must start with '# Review: {source_name}', "
                        f"then one markdown bullet per issue in the pipe-separated "
                        f"schema: '- [SEVERITY] <{source_name}:line> | <category> | "
                        f"<description> | <suggested fix>'. SEVERITY is one of "
                        f"ERROR/WARN/INFO. If no material issues, emit exactly one "
                        f"line: '- [INFO] {source_name}:0 | clean | No material "
                        f"issues | —'.",
            tools=f"1) write_file path={out_path!r} content=<the review markdown>. "
                  f"2) mark_complete summary='reviewed {source_name}' "
                  f"artifacts=[{out_path!r}].",
        ),
        postconditions=(
            _postcond_file_exists(out_path),
            _postcond_review_schema(out_path),
            _postcond_mark_complete(),
        ),
        output_path=out_path,
    )


def build_tasks(source_files: list[str]) -> list[Task]:
    return [_review_task(name) for name in source_files]


def build_staged_tasks(
    tasks: list[Task], source_files: list[str]
) -> dict[str, "StagedTask"]:
    """Stage every review task with the file pre-loaded + schema in the instruction."""
    from agora.fleet.stage_runner import Stage, StagedTask

    by_id = {t.id: t for t in tasks}
    staged: dict[str, StagedTask] = {}

    for source_name in source_files:
        review_name = f"review_{source_name.replace('.', '_')}"
        if review_name not in by_id:
            continue
        out_path = f"review/{source_name}.md"
        staged[review_name] = StagedTask(
            task=by_id[review_name],
            stages=[
                Stage(
                    name=f"review_{source_name}",
                    instruction=(
                        f"Review {source_name}. The file `{out_path}` already "
                        f"exists with its header. Your job is to APPEND one "
                        f"markdown bullet per issue you find.\n\n"
                        f"Schema for each bullet (exactly 4 pipes):\n"
                        f"- [SEVERITY] {source_name}:LINE | CATEGORY | DESCRIPTION | FIX\n\n"
                        f"SEVERITY is ERROR or WARN or INFO. CATEGORY is one of: "
                        f"correctness, dead-code, idiom, typing, testing, style. "
                        f"LINE is an integer. DESCRIPTION and FIX are one sentence "
                        f"each, no pipes, no newlines. Use '-' for FIX when N/A.\n\n"
                        f"Look for: mis-cased identifiers, typos, unused "
                        f"variables/imports, shadowed names, weak test assertions, "
                        f"control-flow bugs. If truly clean, append this one bullet:\n"
                        f"- [INFO] {source_name}:0 | clean | No material issues | -\n\n"
                        f"Call ONE tool: edit_file_append(path='{out_path}', "
                        f"snippet='<your bullets, one per line>'). Do NOT call "
                        f"write_file. Do NOT re-emit the header."
                    ),
                    context_files=(source_name,),
                    max_iterations=5,
                ),
            ],
        )
    return staged


# ---------------------------------------------------------------------- workspace prep

def copy_sources_into_review_workspace(target: Path, review_dir: Path) -> list[str]:
    """Copy every ``.py`` file (non-recursive) from ``target`` into ``review_dir``.

    Returns the list of file names that were copied (basenames), which drives
    the task DAG. Skips ``.git`` and ``__pycache__``. The review DAG writes
    under ``review_dir/review/`` so source + review stay adjacent.

    Each review file is pre-created with its ``# Review: <name>`` header +
    blank line. The review task then only needs to ``edit_file_append`` the
    bullets, which is a much smaller instruction than asking the model to
    emit the full document — critical on 7B models that EOS-bail on long
    prompts containing few-shot examples.
    """
    review_dir.mkdir(parents=True, exist_ok=True)
    out_subdir = review_dir / "review"
    out_subdir.mkdir(exist_ok=True)

    copied: list[str] = []
    for path in sorted(target.glob("*.py")):
        if path.name.startswith("."):
            continue
        shutil.copy2(path, review_dir / path.name)
        header_path = out_subdir / f"{path.name}.md"
        header_path.write_text(
            f"# Review: {path.name}\n\n", encoding="utf-8"
        )
        copied.append(path.name)
    return copied


def aggregate_report(review_dir: Path, source_files: list[str]) -> Path:
    """Concatenate every ``review/<name>.md`` into a single ``review/REPORT.md``
    sorted roughly by severity (ERROR > WARN > INFO) within each file.

    Happens in Python, not through the orchestrator — pure string wrangling
    doesn't need an LLM.
    """
    import re

    _issue_re = re.compile(
        r"^\s*-\s*\[(ERROR|WARN|INFO)\]\s*(.+?)\s*$", re.MULTILINE
    )
    severity_rank = {"ERROR": 0, "WARN": 1, "INFO": 2}

    sections: list[str] = ["# Code review — aggregate report", ""]
    total = {"ERROR": 0, "WARN": 0, "INFO": 0}

    for name in source_files:
        file_path = review_dir / "review" / f"{name}.md"
        if not file_path.is_file():
            sections.append(f"## {name}\n\n_(review file missing)_\n")
            continue
        body = file_path.read_text(encoding="utf-8", errors="replace")
        issues = _issue_re.findall(body)
        issues.sort(key=lambda pair: severity_rank.get(pair[0], 3))
        for sev, _ in issues:
            total[sev] = total.get(sev, 0) + 1
        sections.append(f"## {name}")
        if not issues:
            sections.append("_(no schema-formatted findings — see per-file review)_")
        else:
            for sev, rest in issues:
                sections.append(f"- **[{sev}]** {rest}")
        sections.append("")

    header = [
        f"_{total['ERROR']} ERROR · {total['WARN']} WARN · {total['INFO']} INFO_",
        "",
    ]
    sections[1:1] = header

    out_path = review_dir / "review" / "REPORT.md"
    out_path.write_text("\n".join(sections), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------- main

async def _preflight() -> None:
    print(f"[*] VRAM check for {LLM_MODEL}...")
    check = await check_model_fits(LLM_MODEL, base_url=OLLAMA_BASE_URL)
    print(f"  {check.reason}")
    raise_if_wont_fit(check, LLM_MODEL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agora code-review runner")
    parser.add_argument(
        "--target",
        default=str(WORK_DIR / "discord-bot-full"),
        help="Directory containing the Python sources to review",
    )
    parser.add_argument(
        "--name",
        default="code-review",
        help="Project name (becomes workspace/<name>/)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    target = Path(args.target).resolve()
    review_dir = WORK_DIR / args.name

    if not target.is_dir():
        print(f"[!] target {target} is not a directory", file=sys.stderr)
        sys.exit(2)

    source_files = copy_sources_into_review_workspace(target, review_dir)
    if not source_files:
        print(f"[!] no .py files found under {target}", file=sys.stderr)
        sys.exit(2)

    print(f"[*] target: {target}")
    print(f"[*] review workspace: {review_dir}")
    print(f"[*] {len(source_files)} source file(s): {', '.join(source_files)}")
    print()

    await _preflight()

    print(f"[*] Logging into Conduit as {SYSTEM_USER}")
    client = AgoraMatrixClient(homeserver=HOMESERVER, user_id=SYSTEM_USER)
    await client.login(SYSTEM_PASSWORD)

    if OBSERVER_USER:
        _orig_create_room = client.create_room

        async def _create_with_observer(
            name, topic="", invite=None, initial_state=None
        ):
            merged = list(invite or [])
            if OBSERVER_USER not in merged:
                merged.append(OBSERVER_USER)
            return await _orig_create_room(
                name=name, topic=topic, invite=merged, initial_state=initial_state
            )

        client.create_room = _create_with_observer  # type: ignore[assignment]

    room_manager = RoomManager(client, homeserver_name=SERVER_NAME)

    def llm_factory(model: str):
        if model.startswith("ollama/"):
            return create_llm_adapter(
                model, base_url=OLLAMA_BASE_URL, timeout_seconds=600.0
            )
        raise RuntimeError(f"unexpected model {model!r}")

    orchestrator = Orchestrator(
        matrix_client=client,
        room_manager=room_manager,
        llm_factory=llm_factory,
        work_dir=str(WORK_DIR),
        homeserver_name=SERVER_NAME,
        max_parallel_agents=MAX_PARALLEL,
        enable_observer=True,
        repo_root=str(WORK_DIR),
        ollama_base_url=OLLAMA_BASE_URL,
        skip_warmup=False,
        warmup_deadline=600.0,
        review_timeout_seconds=REVIEW_TIMEOUT,
        enable_web_fetch=False,
        auto_hooks_enabled=True,
    )

    print(f"[*] Running project '{args.name}' (observer enabled)")
    print(f"   max_task_retries={MAX_TASK_RETRIES}  max_parallel={MAX_PARALLEL}")
    print()
    try:
        tasks = build_tasks(source_files)
        staged = build_staged_tasks(tasks, source_files)
        result = await orchestrator.run_project(
            args.name,
            build_agents(),
            tasks,
            max_loopbacks=1,
            staged_tasks=staged,
            max_task_retries=MAX_TASK_RETRIES,
        )
    finally:
        await client.close()

    print("\n" + "=" * 72)
    print(f"Project phase: {result.project.phase.value}")
    print(f"Success: {result.success}")
    print(f"Duration: {result.duration_seconds:.1f}s")
    print(f"Tokens: in={result.total_tokens.get('input_tokens', 0)}, "
          f"out={result.total_tokens.get('output_tokens', 0)}")
    for r in result.task_results:
        mark = "OK" if r.success else "FAIL"
        print(f"  [{mark}] {r.task_id}: {r.iterations} iter")

    # Aggregation runs regardless of per-file success — partial reports are
    # still useful for debugging.
    report = aggregate_report(review_dir, source_files)
    print(f"\n[*] aggregate report: {report}")


if __name__ == "__main__":
    asyncio.run(main())
