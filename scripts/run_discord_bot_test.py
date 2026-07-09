"""Autonomous test run: instruct Agora to build a small Discord bot.

Kicks off an architect → implementer → tester pipeline against the live Conduit +
Ollama stack. Observer layer is enabled, so open Element as ``@fabs:agora.local``
to watch phase banners, task cards, and the REVIEW-phase poll.

Run with:

    .venv/Scripts/python.exe scripts/run_discord_bot_test.py
    AGORA_PROFILE=qwen-coder-32b-p40 .venv/Scripts/python.exe scripts/run_discord_bot_test.py

The primary configuration source is ``profiles.yaml`` at the repo root.
Set ``AGORA_PROFILE=<name>`` to pick one; with no profile selected, the
default from ``profiles.yaml`` is used (and if no profiles.yaml exists,
the packaged default falls back to the historical
``ollama/qwen2.5:7b-instruct`` setup).

Per-field env overrides remain as a secondary escape hatch — see
:func:`agora.fleet.profiles.apply_env_overrides`. The most useful ones:

    AGORA_LLM_MODEL=ollama/qwen2.5-coder:7b   # override profile.model
    AGORA_LLM_NUM_CTX=32768                   # override profile.num_ctx
    AGORA_LLM_MAX_TOKENS=8192                 # override profile.max_tokens
    AGORA_OLLAMA_BASE_URL=http://localhost:11434
    AGORA_OLLAMA_KEEP_ALIVE=1h
    AGORA_VRAM_SAFETY_MARGIN_MIB=1024

Other knobs (unchanged):

    AGORA_MATRIX_HOMESERVER=http://localhost:6167
    AGORA_REVIEW_TIMEOUT_SECONDS=300     # auto-approve after 5min if no poll click
    AGORA_MAX_PARALLEL_AGENTS=2
    AGORA_MAX_TASK_RETRIES=2              # in-phase auto-retries per failed task
"""

from __future__ import annotations

import asyncio
import io
import sys
import uuid
from pathlib import Path

# Windows cp1252 cannot encode many characters LLMs produce; force UTF-8.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
# Silence matrix-nio noise, keep Agora diagnostics.
logging.getLogger("nio").setLevel(logging.WARNING)

# Repo-relative imports without installing as a package.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.config import env_layer, get_settings
from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.orchestrator import Orchestrator
from agora.fleet.profiles import (
    apply_env_overrides,
    build_llm_factory,
    load_profiles,
    resolve_base_url,
)
from agora.fleet.runtime_postconditions import (
    postcond_bot_calls_tree_sync,
    postcond_no_code_after_main_block,
    postcond_pytest_passes,
    postcond_python_imports,
    postcond_readme_only_references_existing_commands,
    postcond_requirements_parse,
)
from agora.fleet.stage_runner import Stage, StagedTask
from agora.matrix.client import AgoraMatrixClient
from agora.matrix.room_manager import RoomManager
from agora.observe.jsonl import (
    RunObserver,
    git_commit_short,
    profile_snapshot_from,
    query_ollama_version,
)
from agora.plan.harness import preflight_vram

# Config comes from one source: Settings (env is read only in config.py). This
# script is a composition root — it reads Settings once and injects typed values.
_settings = get_settings()
HOMESERVER = _settings.matrix_homeserver
SERVER_NAME = _settings.matrix_server_name
SYSTEM_USER = _settings.matrix_user_id
SYSTEM_PASSWORD = _settings.matrix_password
OBSERVER_USER = _settings.observer_user
REVIEW_TIMEOUT = _settings.review_timeout_seconds
MAX_PARALLEL = _settings.max_parallel_agents
MAX_TASK_RETRIES = _settings.max_task_retries
# The orchestrator places each project at ``WORK_DIR / <project_name>`` and uses
# that same directory as the git working tree, so we set WORK_DIR to the
# workspace root. Project "discord-bot" will materialise at workspace/discord-bot/.
WORK_DIR = REPO_ROOT / "workspace"
REPO_ROOT_DIR = WORK_DIR  # retained for backwards-compat kwarg; unified with work_dir.
KB_CACHE_DIR = WORK_DIR / ".knowledge"


def _always_true(_ctx):
    return True, ""


def _require(name: str, check):
    return make_predicate(name, name, check)


def _postcond_file_exists(rel: str):
    def check(ctx):
        artifacts = ctx.get("artifacts") or []
        return (
            any(rel in a for a in artifacts),
            f"expected a recorded artifact containing {rel!r}",
        )

    return _require(f"artifact_contains_{rel.replace('/', '_')}", check)


def _postcond_mark_complete():
    def check(ctx):
        return (bool(ctx.get("completions")), "mark_complete was not called")

    return _require("mark_complete_called", check)


def _postcond_file_contains(rel: str, substring: str):
    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (False, f"could not read {rel}: {exc}")
        return (substring in body, f"{rel} does not contain {substring!r}")

    safe_sub = substring.replace(" ", "_").replace(".", "_")
    return _require(f"{rel.replace('/', '_')}_has_{safe_sub}"[:60], check)


def _postcond_py_compiles(rel: str):
    """Python file must parse AND have no module-scope undefined names.

    Catches two bug classes we've observed in live runs:
      - hallucinated syntax (e.g. ``coroutine def``) via py_compile
      - runtime ``NameError`` at module scope (e.g. ``os.environ`` without
        ``import os``) via the AST-based check in
        :func:`agora.fleet.inner_tools._find_module_scope_undefined_names`
    """
    import py_compile

    from agora.fleet.inner_tools import _find_module_scope_undefined_names

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            return (False, f"{rel} failed py_compile: {str(exc.msg).strip()[:200]}")
        except SyntaxError as exc:
            return (False, f"{rel} SyntaxError at line {exc.lineno}: {exc.msg}")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"could not read {rel}: {exc}")
        undefined = _find_module_scope_undefined_names(source)
        if undefined:
            preview = ", ".join(f"{n}@L{ln}" for n, ln in undefined[:5])
            return (False, f"{rel} has undefined module-scope name(s): {preview}")
        return (True, "")

    return _require(f"{rel.replace('/', '_')}_py_compiles"[:60], check)


ARCHITECT_INSTRUCTIONS = """\
You are the ARCHITECT. Follow each task's description literally — one concrete
action per task. Always finish with `mark_complete(summary=..., artifacts=[...])`.
Prefer calling a single tool at a time.
"""

IMPLEMENTER_INSTRUCTIONS = """\
You are the IMPLEMENTER. Focus on ONE thing per task: write the requested file
with `write_file`. The framework handles validation, git commits, and task
completion automatically.

If a previous `write_file` returned a validation error on the next turn (syntax,
import, or missing name), read the file and re-write it with the fix.
"""

TESTER_INSTRUCTIONS = """\
You are the TESTER. Focus on ONE thing per task: write the requested test
file with `write_file`. The framework handles validation and completion
automatically.

If a previous `write_file` returned a validation error (syntax or failing
pytest), read the file and re-write it with the fix.
"""


def _step(
    step: str,
    inputs: str,
    output: str,
    requirement: str,
    tools: str,
) -> str:
    """Format a task description with the STEP/INPUTS/OUTPUT/REQUIREMENT/TOOLS template."""
    return (
        f"STEP: {step}\n"
        f"INPUTS: {inputs}\n"
        f"OUTPUT: {output}\n"
        f"REQUIREMENT: {requirement}\n"
        f"TOOLS: {tools}\n"
        f'Always call mark_complete with artifacts=["{output}"] when done.'
    )


def build_agents(model: str) -> list[AgentConfig]:
    return [
        AgentConfig(
            name="architect",
            role=AgentRole.ARCHITECT,
            model=model,
            instructions=ARCHITECT_INSTRUCTIONS,
        ),
        AgentConfig(
            name="impl",
            role=AgentRole.IMPLEMENTER,
            model=model,
            instructions=IMPLEMENTER_INSTRUCTIONS,
        ),
        AgentConfig(
            name="tester",
            role=AgentRole.TESTER,
            model=model,
            instructions=TESTER_INSTRUCTIONS,
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
            description=f"{tid}: narrow single-action step",
        ),
        description=description,
        agent_id=agent,
        depends_on=depends_on,
        status=TaskStatus.PENDING,
        output_path=output_path,
    )


def build_tasks() -> list[Task]:
    tasks: list[Task] = []

    # --- 1. fetch_intro ---
    tasks.append(
        _task(
            "fetch_intro",
            "architect",
            _step(
                step="Fetch the discord.py intro docs page and save it locally.",
                inputs="URL https://discordpy.readthedocs.io/en/stable/intro.html",
                output="kb/intro.md",
                requirement="kb/intro.md must exist and contain real documentation text.",
                tools="1) fetch_url url=https://discordpy.readthedocs.io/en/stable/intro.html "
                "save_as=kb/intro.md  (this writes the fetched text to kb/intro.md "
                "in one call — do NOT call write_file separately). "
                "2) mark_complete summary='fetched intro' artifacts=['kb/intro.md'].",
            ),
            postconditions=(
                _postcond_file_exists("kb/intro.md"),
                _postcond_mark_complete(),
            ),
            output_path="kb/intro.md",
        )
    )

    # --- 2. fetch_commands ---
    tasks.append(
        _task(
            "fetch_commands",
            "architect",
            _step(
                step="Fetch the discord.py slash-commands (interactions) reference and save it locally.",
                inputs="URL https://discordpy.readthedocs.io/en/stable/interactions/api.html",
                output="kb/commands.md",
                requirement="kb/commands.md must exist and mention `app_commands` or `tree.command`.",
                tools="1) fetch_url url=https://discordpy.readthedocs.io/en/stable/interactions/api.html "
                "save_as=kb/commands.md  (this writes the fetched text to kb/commands.md "
                "in one call — do NOT call write_file separately). "
                "2) mark_complete summary='fetched commands api' artifacts=['kb/commands.md'].",
            ),
            postconditions=(
                _postcond_file_exists("kb/commands.md"),
                _postcond_mark_complete(),
            ),
            output_path="kb/commands.md",
        )
    )

    # --- 3. design_modules ---
    tasks.append(
        _task(
            "design_modules",
            "architect",
            _step(
                step="Write a short module layout for the Discord bot project.",
                inputs="Read kb/intro.md and kb/commands.md.",
                output="design/modules.md",
                requirement="Must list files: bot.py (entry point), requirements.txt, README.md, "
                "test_bot.py. One line per file, with a 1-sentence purpose.",
                tools="1) read_file path=kb/intro.md. 2) read_file path=kb/commands.md. "
                "3) write_file path=design/modules.md content=<markdown with a bulleted file list>. "
                "4) mark_complete summary='modules' artifacts=['design/modules.md'].",
            ),
            postconditions=(
                _postcond_file_exists("design/modules.md"),
                _postcond_file_contains("design/modules.md", "bot.py"),
                _postcond_mark_complete(),
            ),
            depends_on=("fetch_intro", "fetch_commands"),
            output_path="design/modules.md",
        )
    )

    # --- 4. design_commands ---
    tasks.append(
        _task(
            "design_commands",
            "architect",
            _step(
                step="Write signatures for the three slash commands.",
                inputs="Read kb/commands.md.",
                output="design/commands.md",
                requirement="Must include three command signatures: /ping (no args), "
                "/roll sides:int=6, /echo text:str. Use discord.py 2.x app_commands idioms.",
                tools="1) read_file path=kb/commands.md. "
                "2) write_file path=design/commands.md content=<markdown listing the three commands>. "
                "3) mark_complete summary='commands' artifacts=['design/commands.md'].",
            ),
            postconditions=(
                _postcond_file_exists("design/commands.md"),
                _postcond_file_contains("design/commands.md", "ping"),
                _postcond_file_contains("design/commands.md", "roll"),
                _postcond_file_contains("design/commands.md", "echo"),
                _postcond_mark_complete(),
            ),
            depends_on=("fetch_intro", "fetch_commands"),
            output_path="design/commands.md",
        )
    )

    # --- (assemble_design removed — build_skeleton reads the two design files directly) ---

    # --- 6. build_skeleton ---
    tasks.append(
        _task(
            "build_skeleton",
            "impl",
            _step(
                step="Write the bot.py skeleton (imports, Intents, Bot/Client, on_ready, __main__).",
                inputs="Read design/modules.md and design/commands.md.",
                output="bot.py",
                requirement="bot.py must import discord, create a commands.Bot or discord.Client with "
                "appropriate Intents, read DISCORD_TOKEN from os.environ, and call "
                "bot.run(TOKEN) under __main__. NO slash commands yet.",
                tools="1) read_file path=design/modules.md. "
                "2) read_file path=design/commands.md. "
                "3) write_file path=bot.py content=<python skeleton>. "
                "4) check_python path=bot.py  (must return 'OK'; if SyntaxError, fix and re-write). "
                "5) git_commit message='feat: bot skeleton'. "
                "6) mark_complete summary='skeleton' artifacts=['bot.py'].",
            ),
            postconditions=(
                _postcond_file_exists("bot.py"),
                _postcond_py_compiles("bot.py"),
                postcond_python_imports("bot.py"),
                postcond_no_code_after_main_block("bot.py"),
                _postcond_file_contains("bot.py", "discord"),
                _postcond_file_contains("bot.py", "DISCORD_TOKEN"),
                _postcond_mark_complete(),
            ),
            depends_on=("design_modules", "design_commands"),
            output_path="bot.py",
        )
    )

    # --- 7. build_ping ---
    tasks.append(
        _task(
            "build_ping",
            "impl",
            _step(
                step="Add a /ping slash command to bot.py.",
                inputs="Read bot.py and design/commands.md.",
                output="bot.py",
                requirement="bot.py must still have the skeleton AND a new /ping handler using "
                "@bot.tree.command or app_commands. /ping replies 'pong'.",
                tools="1) read_file path=bot.py. 2) read_file path=design/commands.md. "
                "3) write_file path=bot.py content=<full updated content>. "
                "4) check_python path=bot.py  (must return 'OK'; re-write on SyntaxError). "
                "5) git_commit message='feat: /ping'. "
                "6) mark_complete summary='/ping added' artifacts=['bot.py'].",
            ),
            postconditions=(
                _postcond_file_exists("bot.py"),
                _postcond_py_compiles("bot.py"),
                postcond_python_imports("bot.py"),
                postcond_no_code_after_main_block("bot.py"),
                _postcond_file_contains("bot.py", "ping"),
                _postcond_file_contains("bot.py", "DISCORD_TOKEN"),
                _postcond_mark_complete(),
            ),
            depends_on=("build_skeleton",),
            output_path="bot.py",
        )
    )

    # --- 8. build_roll ---
    tasks.append(
        _task(
            "build_roll",
            "impl",
            _step(
                step="Add a /roll slash command to bot.py.",
                inputs="Read bot.py.",
                output="bot.py",
                requirement="bot.py must keep /ping AND add /roll with an int parameter `sides` "
                "defaulting to 6. It replies with a random int between 1 and sides.",
                tools="1) read_file path=bot.py. "
                "2) write_file path=bot.py content=<full updated content>. "
                "3) check_python path=bot.py  (must return 'OK'; re-write on SyntaxError). "
                "4) git_commit message='feat: /roll'. "
                "5) mark_complete summary='/roll added' artifacts=['bot.py'].",
            ),
            postconditions=(
                _postcond_file_exists("bot.py"),
                _postcond_py_compiles("bot.py"),
                postcond_python_imports("bot.py"),
                postcond_no_code_after_main_block("bot.py"),
                _postcond_file_contains("bot.py", "ping"),
                _postcond_file_contains("bot.py", "roll"),
                _postcond_mark_complete(),
            ),
            depends_on=("build_ping",),
            output_path="bot.py",
        )
    )

    # --- 9. build_echo ---
    tasks.append(
        _task(
            "build_echo",
            "impl",
            _step(
                step="Add a /echo slash command to bot.py.",
                inputs="Read bot.py.",
                output="bot.py",
                requirement="bot.py must keep /ping + /roll AND add /echo with a str parameter `text` "
                "that echoes the user's text.",
                tools="1) read_file path=bot.py. "
                "2) write_file path=bot.py content=<full updated content>. "
                "3) check_python path=bot.py  (must return 'OK'; re-write on SyntaxError). "
                "4) git_commit message='feat: /echo'. "
                "5) mark_complete summary='/echo added' artifacts=['bot.py'].",
            ),
            postconditions=(
                _postcond_file_exists("bot.py"),
                _postcond_py_compiles("bot.py"),
                postcond_python_imports("bot.py"),
                postcond_no_code_after_main_block("bot.py"),
                postcond_bot_calls_tree_sync("bot.py"),
                _postcond_file_contains("bot.py", "ping"),
                _postcond_file_contains("bot.py", "roll"),
                _postcond_file_contains("bot.py", "echo"),
                _postcond_mark_complete(),
            ),
            depends_on=("build_roll",),
            output_path="bot.py",
        )
    )

    # --- 10. write_requirements (parallel with 7-9 after skeleton exists) ---
    tasks.append(
        _task(
            "write_requirements",
            "impl",
            _step(
                step="Write requirements.txt with discord.py 2.x pinned.",
                inputs="None.",
                output="requirements.txt",
                requirement="requirements.txt must contain a single line pinning discord.py>=2.3.",
                tools="1) write_file path=requirements.txt content='discord.py>=2.3\\n'. "
                "2) git_commit message='chore: requirements'. "
                "3) mark_complete summary='requirements' artifacts=['requirements.txt'].",
            ),
            postconditions=(
                _postcond_file_exists("requirements.txt"),
                _postcond_file_contains("requirements.txt", "discord.py"),
                postcond_requirements_parse("requirements.txt"),
                _postcond_mark_complete(),
            ),
            depends_on=("build_skeleton",),
            output_path="requirements.txt",
        )
    )

    # --- 11. write_readme (depends only on skeleton; not on any /command tasks) ---
    tasks.append(
        _task(
            "write_readme",
            "impl",
            _step(
                step="Write a short README.md explaining how to run the bot.",
                inputs="Read bot.py (only the skeleton is guaranteed to exist; "
                "individual /commands may or may not be implemented yet).",
                output="README.md",
                requirement="README.md must mention DISCORD_TOKEN env var and `python bot.py`. "
                "Describe usage of whatever commands exist — do NOT reference "
                "specific commands that aren't visible in bot.py.",
                tools="1) read_file path=bot.py. "
                "2) write_file path=README.md content=<short markdown>. "
                "3) git_commit message='docs: README'. "
                "4) mark_complete summary='readme' artifacts=['README.md'].",
            ),
            postconditions=(
                _postcond_file_exists("README.md"),
                _postcond_file_contains("README.md", "DISCORD_TOKEN"),
                postcond_readme_only_references_existing_commands(),
                _postcond_mark_complete(),
            ),
            depends_on=("build_skeleton",),
            output_path="README.md",
        )
    )

    # --- 12. write_tests (depends only on skeleton + requirements) ---
    tasks.append(
        _task(
            "write_tests",
            "tester",
            _step(
                step="Write test_bot.py that exercises whatever commands bot.py registers.",
                inputs="Read bot.py and requirements.txt. Only the skeleton is guaranteed; "
                "individual /commands may or may not be implemented.",
                output="test_bot.py",
                requirement="test_bot.py must contain at least one `def test_` function and use "
                "unittest.mock. Tests must NOT require a real Discord token. "
                "Introspect the bot's command tree (e.g. via "
                "`bot.bot.tree.get_commands()`) rather than hard-coding /ping/roll/echo.",
                tools="1) read_file path=bot.py. 2) read_file path=requirements.txt. "
                "3) write_file path=test_bot.py content=<pytest code with mocks>. "
                "4) check_python path=test_bot.py  (must return 'OK'; re-write on SyntaxError). "
                "5) git_commit message='test: bot command handlers'. "
                "6) mark_complete summary='tests' artifacts=['test_bot.py'].",
            ),
            postconditions=(
                _postcond_file_exists("test_bot.py"),
                _postcond_py_compiles("test_bot.py"),
                _postcond_file_contains("test_bot.py", "def test_"),
                postcond_pytest_passes("test_bot.py"),
                _postcond_mark_complete(),
            ),
            depends_on=("build_skeleton", "write_requirements"),
            output_path="test_bot.py",
        )
    )

    # --- 13. integration_check (terminal gate — postconditions do the real work) ---
    tasks.append(
        _task(
            "integration_check",
            "tester",
            _step(
                step="Confirm the repo is complete. DO NOT modify any files.",
                inputs="The entire workspace.",
                output="(no new file — this task only verifies)",
                requirement="Just call mark_complete. Gate postconditions run automatically "
                "and verify bot.py imports, requirements.txt parses, test_bot.py "
                "passes pytest, bot.py calls tree.sync, and README.md only "
                "references commands that exist in bot.py.",
                tools="1) mark_complete summary='integration OK' artifacts=[].",
            ),
            postconditions=(
                postcond_python_imports("bot.py"),
                postcond_no_code_after_main_block("bot.py"),
                postcond_requirements_parse("requirements.txt"),
                postcond_pytest_passes("test_bot.py"),
                postcond_bot_calls_tree_sync("bot.py"),
                postcond_readme_only_references_existing_commands(),
                _postcond_mark_complete(),
            ),
            depends_on=("build_echo", "write_requirements", "write_readme", "write_tests"),
        )
    )

    return tasks


def build_staged_tasks(tasks: list[Task]) -> dict[str, StagedTask]:
    """Stage the tasks where weak models consistently fail.

    Tasks NOT staged still run through the normal one-shot execute_task loop —
    staging is only applied where the model has demonstrably hallucinated on
    unstaged runs (``write_requirements``, ``build_skeleton``, ``build_ping``,
    ``build_roll``, ``build_echo``, ``write_tests``).
    """
    by_id = {t.id: t for t in tasks}
    staged: dict[str, StagedTask] = {}

    # write_requirements: the single line is dictated, no room to hallucinate.
    if "write_requirements" in by_id:
        staged["write_requirements"] = StagedTask(
            task=by_id["write_requirements"],
            stages=[
                Stage(
                    name="write",
                    instruction=(
                        "Write the file `requirements.txt` with EXACTLY this content "
                        "(one line, no import statements, no comments):\n"
                        "discord.py>=2.3\n\n"
                        "Call write_file path='requirements.txt' content='discord.py>=2.3\\n'."
                    ),
                    max_iterations=4,
                ),
            ],
        )

    # build_skeleton: a literal template, discord.Intents (not commands.Intents),
    # command_prefix required, module-scope DISCORD_TOKEN with import os.
    if "build_skeleton" in by_id:
        staged["build_skeleton"] = StagedTask(
            task=by_id["build_skeleton"],
            stages=[
                Stage(
                    name="write_bot_skeleton",
                    instruction=(
                        "Write `bot.py` with this exact structure:\n\n"
                        "```python\n"
                        "import os\n"
                        "import discord\n"
                        "from discord.ext import commands\n"
                        "\n"
                        "DISCORD_TOKEN = os.environ['DISCORD_TOKEN']\n"
                        "\n"
                        "intents = discord.Intents.default()\n"
                        "intents.message_content = True\n"
                        "bot = commands.Bot(command_prefix='!', intents=intents)\n"
                        "\n"
                        "@bot.event\n"
                        "async def on_ready():\n"
                        "    await bot.tree.sync()\n"
                        "    print(f'{bot.user} ready')\n"
                        "\n"
                        "if __name__ == '__main__':\n"
                        "    bot.run(DISCORD_TOKEN)\n"
                        "```\n\n"
                        "Use discord.Intents (NOT commands.Intents). "
                        "Use command_prefix='!' (required). "
                        "Call write_file path='bot.py' content=<the code above verbatim>."
                    ),
                    context_files=("design/modules.md", "design/commands.md"),
                    max_iterations=6,
                ),
            ],
        )

    # build_ping: edit_file_insert_before — model only emits the new snippet.
    if "build_ping" in by_id:
        staged["build_ping"] = StagedTask(
            task=by_id["build_ping"],
            stages=[
                Stage(
                    name="add_ping",
                    instruction=(
                        "Add a /ping slash command to bot.py. Call ONE tool:\n\n"
                        "edit_file_insert_before(\n"
                        "    path='bot.py',\n"
                        '    anchor="if __name__",\n'
                        '    snippet="""\n'
                        "@bot.tree.command(name='ping', description='pong')\n"
                        "async def ping(interaction: discord.Interaction):\n"
                        "    await interaction.response.send_message('pong')\n"
                        "\n"
                        '""",\n'
                        ")\n\n"
                        "Do NOT call write_file. Do NOT re-emit existing bot.py content. "
                        "The anchor string 'if __name__' already appears in bot.py; "
                        "edit_file_insert_before will place your snippet right above it."
                    ),
                    context_files=("bot.py",),
                    max_iterations=5,
                ),
            ],
        )

    # build_roll: two insertions — `import random` up top, then the handler.
    if "build_roll" in by_id:
        staged["build_roll"] = StagedTask(
            task=by_id["build_roll"],
            stages=[
                Stage(
                    name="add_roll",
                    instruction=(
                        "Add a /roll slash command to bot.py. Call TWO tools in order:\n\n"
                        "1) edit_file_insert_before(\n"
                        "     path='bot.py',\n"
                        "     anchor='import discord',\n"
                        "     snippet='import random\\n',\n"
                        "   )\n\n"
                        "2) edit_file_insert_before(\n"
                        "     path='bot.py',\n"
                        '     anchor="if __name__",\n'
                        '     snippet="""\n'
                        "@bot.tree.command(name='roll', description='roll a dN')\n"
                        "async def roll(interaction: discord.Interaction, sides: int = 6):\n"
                        "    await interaction.response.send_message(str(random.randint(1, sides)))\n"
                        "\n"
                        '""",\n'
                        "   )\n\n"
                        "Use the standard-library `random` module (NOT discord.utils.random). "
                        "Do NOT call write_file. Do NOT call edit_file_append — "
                        "appending a handler after `if __name__` makes it unreachable at "
                        "runtime (bot.run blocks). Always use edit_file_insert_before "
                        "with anchor='if __name__' for handler insertions."
                    ),
                    context_files=("bot.py",),
                    max_iterations=6,
                ),
            ],
        )

    # build_echo: single insertion of the /echo decorator block.
    if "build_echo" in by_id:
        staged["build_echo"] = StagedTask(
            task=by_id["build_echo"],
            stages=[
                Stage(
                    name="add_echo",
                    instruction=(
                        "Add a /echo slash command to bot.py. Call ONE tool:\n\n"
                        "edit_file_insert_before(\n"
                        "    path='bot.py',\n"
                        '    anchor="if __name__",\n'
                        '    snippet="""\n'
                        "@bot.tree.command(name='echo', description='echo text')\n"
                        "async def echo(interaction: discord.Interaction, text: str):\n"
                        "    await interaction.response.send_message(text)\n"
                        "\n"
                        '""",\n'
                        ")\n\n"
                        "Do NOT call write_file. Do NOT call edit_file_append — "
                        "handlers MUST appear BEFORE `if __name__` or they won't "
                        "register at runtime (bot.run blocks)."
                    ),
                    context_files=("bot.py",),
                    max_iterations=5,
                ),
            ],
        )

    # write_tests: introspect bot, write minimal pytest that calls callbacks directly.
    if "write_tests" in by_id:
        staged["write_tests"] = StagedTask(
            task=by_id["write_tests"],
            stages=[
                Stage(
                    name="write_tests",
                    instruction=(
                        "Write `test_bot.py` with pytest-style tests that exercise the "
                        "commands registered on bot.bot.tree. Use this template:\n\n"
                        "```python\n"
                        "from unittest.mock import AsyncMock\n"
                        "import asyncio\n"
                        "import bot\n"
                        "\n"
                        "def _names() -> set[str]:\n"
                        "    return {c.name for c in bot.bot.tree.get_commands()}\n"
                        "\n"
                        "def test_ping_registered():\n"
                        "    assert 'ping' in _names()\n"
                        "\n"
                        "def test_ping_callback_sends_pong():\n"
                        "    cmd = next(c for c in bot.bot.tree.get_commands() if c.name == 'ping')\n"
                        "    interaction = AsyncMock()\n"
                        "    asyncio.run(cmd.callback(interaction))\n"
                        "    interaction.response.send_message.assert_called_once_with('pong')\n"
                        "```\n\n"
                        "Note: `@bot.tree.command` wraps the function in a Command object. "
                        "Invoke its `.callback(interaction)` attribute — the Command itself "
                        "is not directly callable. "
                        "Call write_file path='test_bot.py' content=<the code above verbatim>."
                    ),
                    context_files=("bot.py",),
                    max_iterations=7,
                ),
            ],
        )

    return staged


async def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    REPO_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    KB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Profile is the single source of truth for model + inference config.
    # AGORA_PROFILE picks a named profile; per-field env overrides
    # (AGORA_LLM_MODEL, AGORA_LLM_NUM_CTX, …) layer on top — see
    # agora.fleet.profiles.apply_env_overrides.
    profile_set = load_profiles(_settings.profiles_file)
    profile = apply_env_overrides(profile_set.select(_settings.profile), env=env_layer())
    print(
        f"[*] Profile: {profile.name or '<unnamed>'} → model={profile.model}, "
        f"num_ctx={profile.num_ctx}, max_tokens={profile.max_tokens}, "
        f"keep_alive={profile.keep_alive}"
    )

    # Resolve the profile's optional endpoint override against the single-source
    # Settings endpoint (profile.ollama.base_url is None ⇒ inherit).
    ollama_base_url = resolve_base_url(profile, _settings.ollama_base_url)

    await preflight_vram(
        profile.model,
        ollama_base_url,
        safety_margin_mib=profile.vram.safety_margin_mib,
    )

    print(f"[*] Logging into Conduit as {SYSTEM_USER}")
    client = AgoraMatrixClient(homeserver=HOMESERVER, user_id=SYSTEM_USER)
    await client.login(SYSTEM_PASSWORD)

    # Auto-invite the observer user to every room we create so Element sees the stream.
    # Not a framework feature — this is a test-harness shim.
    if OBSERVER_USER:
        _orig_create_room = client.create_room

        async def _create_with_observer(name, topic="", invite=None, initial_state=None):
            merged = list(invite or [])
            if OBSERVER_USER not in merged:
                merged.append(OBSERVER_USER)
            return await _orig_create_room(
                name=name, topic=topic, invite=merged, initial_state=initial_state
            )

        client.create_room = _create_with_observer  # type: ignore[assignment]
        print(f"[*] Auto-inviting {OBSERVER_USER} to every created room")

    room_manager = RoomManager(client, homeserver_name=SERVER_NAME)
    llm_factory = build_llm_factory(profile, ollama_base_url)

    # Structured run logging (JSONL schema v1). Emits run.jsonl + tasks.jsonl
    # into AGORA_RUN_OUTPUT_DIR (default runs_out/_default/<run_id>/). This is
    # the Checkpoint-1 reproduction case.
    run_id = uuid.uuid4().hex
    output_dir = RunObserver.resolve_output_dir(run_id)
    observer = RunObserver(
        run_id=run_id,
        output_dir=output_dir,
        probe_name="discord-bot",
        flow_path="scripts/run_discord_bot_test.py",
        project_name="discord-bot",
        profile=profile_snapshot_from(profile),
        ollama_version=query_ollama_version(ollama_base_url),
        git_commit=git_commit_short(REPO_ROOT),
    )
    print(f"[*] Run observer → {output_dir} (run_id={run_id})")

    orchestrator = Orchestrator(
        matrix_client=client,
        room_manager=room_manager,
        llm_factory=llm_factory,
        work_dir=str(WORK_DIR),
        homeserver_name=SERVER_NAME,
        max_parallel_agents=MAX_PARALLEL,
        enable_observer=True,
        repo_root=str(REPO_ROOT_DIR),
        knowledge_cache_dir=str(KB_CACHE_DIR),
        ollama_base_url=ollama_base_url,
        skip_warmup=False,
        warmup_deadline=600.0,
        keep_alive=profile.keep_alive,
        review_timeout_seconds=REVIEW_TIMEOUT,
        enable_web_fetch=True,
        fetch_timeout_seconds=30.0,
        fetch_max_bytes=1_048_576,
        fetch_max_text_bytes=65_536,
        auto_hooks_enabled=True,
        observer=observer,
    )

    print("[*] Running project 'discord-bot' (observer enabled)")
    print("   open Element as @fabs:agora.local to watch and vote on the REVIEW poll")
    print(f"   review_timeout_seconds={REVIEW_TIMEOUT} (auto-decides if you don't click)")
    print()
    try:
        tasks = build_tasks()
        staged = build_staged_tasks(tasks)
        result = await orchestrator.run_project(
            "discord-bot",
            build_agents(profile.model),
            tasks,
            max_loopbacks=2,
            staged_tasks=staged,
            max_task_retries=MAX_TASK_RETRIES,
        )
    finally:
        await client.close()

    print("\n" + "=" * 72)
    print(f"Project phase: {result.project.phase.value}")
    print(f"Success: {result.success}")
    print(f"Project room: {result.project_room_id}")
    print(f"Duration: {result.duration_seconds:.1f}s")
    print(
        f"Tokens: in={result.total_tokens.get('input_tokens', 0)}, "
        f"out={result.total_tokens.get('output_tokens', 0)}"
    )
    for r in result.task_results:
        mark = "OK" if r.success else "FAIL"
        print(f"  [{mark}] {r.task_id}: {r.iterations} iter  -> {r.output[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
