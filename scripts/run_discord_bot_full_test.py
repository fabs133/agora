"""Autonomous stress test: Discord bot that mirrors Agora's `/agora` command
surface 1:1 via a Matrix bridge.

This is the scaled-up sibling of ``run_discord_bot_test.py``. The reference
runners built a 3-command bot in 13 tasks; this one builds an 8-command,
multi-module bot in 24 tasks. The shape stresses the framework in ways the
prior runs did not:

- **Multi-module** — bot.py, matrix_bridge.py, config.py plus two test files.
  Cross-module imports must stay consistent across the run.
- **Repetitive-but-distinct handlers** — 8 slash command tasks that look
  structurally similar. A model that pattern-collapses will reproduce the
  same mistake eight times; this exercises auto-retry + auto-learning as
  much as it tests code generation.
- **Network bridge** — matrix-nio ``AsyncClient`` for the bot→Agora path.
  Tests must mock the client; production logs in for real.

The bot does NOT touch Agora internals directly. It opens a Matrix session
and posts the literal ``/agora <verb> <args>`` text into the project room —
the existing observer/command dispatch then handles it normally.

Run with:

    .venv/Scripts/python.exe scripts/run_discord_bot_full_test.py

Environment knobs (all optional, sensible defaults):

    AGORA_MATRIX_HOMESERVER=http://localhost:6167
    AGORA_OLLAMA_BASE_URL=http://localhost:11434
    AGORA_LLM_MODEL=ollama/qwen2.5:7b-instruct
    AGORA_REVIEW_TIMEOUT_SECONDS=300
    AGORA_MAX_PARALLEL_AGENTS=2
    AGORA_MAX_TASK_RETRIES=2
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

# Windows cp1252 can't encode the characters LLMs produce. Force UTF-8.
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
from agora.fleet.runtime_postconditions import (
    postcond_bot_calls_tree_sync,
    postcond_no_code_after_main_block,
    postcond_pytest_passes,
    postcond_python_imports,
    postcond_readme_only_references_existing_commands,
    postcond_requirements_parse,
)
from agora.fleet.stage_runner import Stage, StagedTask
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
MAX_PARALLEL = int(os.getenv("AGORA_MAX_PARALLEL_AGENTS", "2"))
MAX_TASK_RETRIES = int(os.getenv("AGORA_MAX_TASK_RETRIES", "2"))
WORK_DIR = REPO_ROOT / "workspace"
REPO_ROOT_DIR = WORK_DIR
KB_CACHE_DIR = WORK_DIR / ".knowledge"


# ---------------------------------------------------------------------- postcond helpers

def _require(name: str, check):
    return make_predicate(name, name, check)


def _postcond_file_exists(rel: str):
    def check(ctx):
        artifacts = ctx.get("artifacts") or []
        return (
            any(rel in a for a in artifacts),
            f"expected a recorded artifact containing {rel!r}",
        )

    return _require(f"artifact_contains_{rel.replace('/', '_')}"[:60], check)


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

    safe_sub = substring.replace(" ", "_").replace(".", "_").replace("/", "_")
    return _require(f"{rel.replace('/', '_')}_has_{safe_sub}"[:60], check)


def _postcond_py_compiles(rel: str):
    """Parse + module-scope undefined-name check — same as the small runner."""
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

    return _require(f"{rel.replace('/', '_').replace('.', '_')}_py_compiles"[:60], check)


def _postcond_all_commands_registered():
    """bot.py must declare all 8 command names via @bot.tree.command(name='...')."""
    import re

    _decl_re = re.compile(r"name\s*=\s*['\"]([a-z][a-z0-9_]*)['\"]")
    expected = frozenset({
        "pause", "resume", "abort", "note",
        "comment", "redirect", "review", "help",
    })

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing")
        path = Path(work_dir) / "bot.py"
        if not path.is_file():
            return (False, "bot.py does not exist under work_dir")
        text = path.read_text(encoding="utf-8", errors="replace")
        declared = set(_decl_re.findall(text))
        missing = sorted(expected - declared)
        if missing:
            return (
                False,
                f"bot.py missing command declarations: {', '.join(missing)}",
            )
        return (True, "")

    return _require("bot_declares_all_eight_commands", check)


# ---------------------------------------------------------------------- agent prompts

ARCHITECT_INSTRUCTIONS = """\
You are the ARCHITECT. Follow each task's description literally — one concrete
action per task. Always finish with `mark_complete(summary=..., artifacts=[...])`.
Prefer calling a single tool at a time.
"""

IMPLEMENTER_INSTRUCTIONS = """\
You are the IMPLEMENTER. Focus on ONE thing per task: write or edit the
requested file. The framework runs check_python, git_commit, and mark_complete
for you automatically after each write.

This project has THREE source modules: config.py, matrix_bridge.py, bot.py.
Do not merge them. Do not re-emit existing file content on an edit task —
use edit_file_insert_before / edit_file_replace / edit_file_append.

If a previous write returned a validation error (syntax, import, or missing
name), read the file and re-write it with the fix.
"""

TESTER_INSTRUCTIONS = """\
You are the TESTER. Focus on ONE thing per task: write the requested test
file with `write_file`. Tests must NEVER hit a real network — mock
`nio.AsyncClient` and use `unittest.mock.AsyncMock` for anything async.

If a previous write returned a validation error (syntax or failing pytest),
read the file and re-write it with the fix.
"""


def _step(
    step: str, inputs: str, output: str, requirement: str, tools: str
) -> str:
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
            name="architect",
            role=AgentRole.ARCHITECT,
            model=LLM_MODEL,
            instructions=ARCHITECT_INSTRUCTIONS,
        ),
        AgentConfig(
            name="impl",
            role=AgentRole.IMPLEMENTER,
            model=LLM_MODEL,
            instructions=IMPLEMENTER_INSTRUCTIONS,
        ),
        AgentConfig(
            name="tester",
            role=AgentRole.TESTER,
            model=LLM_MODEL,
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


# ---------------------------------------------------------------------- command map

# The single source of truth for (discord slash name → agora verb → signature).
# Used both to generate the design-spec prompt and to drive the eight staged
# build_cmd_* tasks so the prompt text and postconditions stay in lockstep.
COMMAND_MAP: tuple[dict[str, str], ...] = (
    {
        "name": "pause",
        "agora_verb": "pause",
        "params": "",
        "description": "pause the orchestrator",
        "body_template": (
            "    await bridge.send_agora_command('/agora pause')\n"
            "    await interaction.response.send_message('sent: /agora pause')"
        ),
    },
    {
        "name": "resume",
        "agora_verb": "resume",
        "params": "",
        "description": "resume the orchestrator",
        "body_template": (
            "    await bridge.send_agora_command('/agora resume')\n"
            "    await interaction.response.send_message('sent: /agora resume')"
        ),
    },
    {
        "name": "abort",
        "agora_verb": "abort",
        "params": ", reason: str = ''",
        "description": "abort the current project",
        "body_template": (
            "    text = '/agora abort ' + reason if reason else '/agora abort'\n"
            "    await bridge.send_agora_command(text)\n"
            "    await interaction.response.send_message('sent: ' + text)"
        ),
    },
    {
        "name": "note",
        "agora_verb": "note",
        "params": ", text: str",
        "description": "attach a note for all agents",
        "body_template": (
            "    payload = '/agora note ' + text\n"
            "    await bridge.send_agora_command(payload)\n"
            "    await interaction.response.send_message('sent: ' + payload)"
        ),
    },
    {
        "name": "comment",
        "agora_verb": "comment",
        "params": ", task_id: str, text: str",
        "description": "per-task feedback",
        "body_template": (
            "    payload = '/agora comment ' + task_id + ' ' + text\n"
            "    await bridge.send_agora_command(payload)\n"
            "    await interaction.response.send_message('sent: ' + payload)"
        ),
    },
    {
        "name": "redirect",
        "agora_verb": "redirect",
        "params": ", agent: str, text: str",
        "description": "rewrite agent instructions",
        "body_template": (
            "    payload = '/agora redirect ' + agent + ' ' + text\n"
            "    await bridge.send_agora_command(payload)\n"
            "    await interaction.response.send_message('sent: ' + payload)"
        ),
    },
    {
        "name": "review",
        "agora_verb": "review",
        "params": ", answer: str",
        "description": "cast a review poll vote",
        "body_template": (
            "    payload = '/agora review ' + answer\n"
            "    await bridge.send_agora_command(payload)\n"
            "    await interaction.response.send_message('sent: ' + payload)"
        ),
    },
    {
        "name": "help",
        "agora_verb": "help",
        "params": "",
        "description": "show Agora help",
        "body_template": (
            "    await bridge.send_agora_command('/agora help')\n"
            "    await interaction.response.send_message('sent: /agora help')"
        ),
    },
)


def _handler_snippet(cmd: dict[str, str]) -> str:
    """Render the literal decorator + coroutine the stage should insert."""
    params = cmd["params"]
    return (
        f"@bot.tree.command(name='{cmd['name']}', description='{cmd['description']}')\n"
        f"async def {cmd['name']}_cmd(interaction: discord.Interaction{params}):\n"
        f"{cmd['body_template']}\n"
    )


# ---------------------------------------------------------------------- task DAG

def build_tasks() -> list[Task]:
    tasks: list[Task] = []

    # --- 1. fetch_intro (discord.py intro) ---
    tasks.append(_task(
        "fetch_intro",
        "architect",
        _step(
            step="Fetch the discord.py intro docs page and save it locally.",
            inputs="URL https://discordpy.readthedocs.io/en/stable/intro.html",
            output="kb/intro.md",
            requirement="kb/intro.md must exist and contain real documentation text.",
            tools="1) fetch_url url=https://discordpy.readthedocs.io/en/stable/intro.html "
                  "save_as=kb/intro.md (writes atomically — do NOT call write_file). "
                  "2) mark_complete summary='fetched intro' artifacts=['kb/intro.md'].",
        ),
        postconditions=(
            _postcond_file_exists("kb/intro.md"),
            _postcond_mark_complete(),
        ),
        output_path="kb/intro.md",
    ))

    # --- 2. fetch_commands (slash commands api) ---
    tasks.append(_task(
        "fetch_commands",
        "architect",
        _step(
            step="Fetch the discord.py slash-commands (interactions) reference.",
            inputs="URL https://discordpy.readthedocs.io/en/stable/interactions/api.html",
            output="kb/commands.md",
            requirement="kb/commands.md must exist and mention app_commands or tree.command.",
            tools="1) fetch_url url=https://discordpy.readthedocs.io/en/stable/interactions/api.html "
                  "save_as=kb/commands.md (writes atomically — do NOT call write_file). "
                  "2) mark_complete summary='fetched commands api' artifacts=['kb/commands.md'].",
        ),
        postconditions=(
            _postcond_file_exists("kb/commands.md"),
            _postcond_mark_complete(),
        ),
        output_path="kb/commands.md",
    ))

    # --- 3. fetch_nio (matrix-nio quickstart) ---
    tasks.append(_task(
        "fetch_nio",
        "architect",
        _step(
            step="Fetch the matrix-nio README/quickstart and save it locally.",
            inputs="URL https://matrix-nio.readthedocs.io/en/latest/nio.html",
            output="kb/nio.md",
            requirement="kb/nio.md must exist and mention AsyncClient and room_send.",
            tools="1) fetch_url url=https://matrix-nio.readthedocs.io/en/latest/nio.html "
                  "save_as=kb/nio.md (writes atomically — do NOT call write_file). "
                  "2) mark_complete summary='fetched nio docs' artifacts=['kb/nio.md'].",
        ),
        postconditions=(
            _postcond_file_exists("kb/nio.md"),
            _postcond_mark_complete(),
        ),
        output_path="kb/nio.md",
    ))

    # --- 4. design_modules ---
    tasks.append(_task(
        "design_modules",
        "architect",
        _step(
            step="Write a short module layout for the full-command Discord bot.",
            inputs="Read kb/intro.md, kb/commands.md, kb/nio.md.",
            output="design/modules.md",
            requirement="Must list files: config.py (env loader), matrix_bridge.py "
                        "(MatrixBridge class that logs into Matrix and sends /agora "
                        "messages), bot.py (entry point + 8 slash commands), "
                        "requirements.txt, README.md, test_matrix_bridge.py, "
                        "test_bot_commands.py. One line per file, with a 1-sentence "
                        "purpose.",
            tools="1) read_file path=kb/intro.md. 2) read_file path=kb/commands.md. "
                  "3) read_file path=kb/nio.md. "
                  "4) write_file path=design/modules.md content=<markdown with bulleted file list>. "
                  "5) mark_complete summary='modules' artifacts=['design/modules.md'].",
        ),
        postconditions=(
            _postcond_file_exists("design/modules.md"),
            _postcond_file_contains("design/modules.md", "bot.py"),
            _postcond_file_contains("design/modules.md", "matrix_bridge.py"),
            _postcond_file_contains("design/modules.md", "config.py"),
            _postcond_mark_complete(),
        ),
        depends_on=("fetch_intro", "fetch_commands", "fetch_nio"),
        output_path="design/modules.md",
    ))

    # --- 5. design_commands_spec ---
    cmd_list_md = "\n".join(
        f"- /{c['name']}{(' ' + c['params'].lstrip(', ')) if c['params'] else ''} "
        f"→ sends `/agora {c['agora_verb']}{' ...' if c['params'] else ''}`"
        for c in COMMAND_MAP
    )
    tasks.append(_task(
        "design_commands_spec",
        "architect",
        _step(
            step="Write the full slash-command spec for the bot (8 commands).",
            inputs="Read kb/commands.md.",
            output="design/commands.md",
            requirement="Must list exactly these eight slash commands and show what "
                        "each one sends via the Matrix bridge:\n" + cmd_list_md,
            tools="1) read_file path=kb/commands.md. "
                  "2) write_file path=design/commands.md content=<markdown listing the 8 commands "
                  "and their /agora payloads>. "
                  "3) mark_complete summary='commands spec' artifacts=['design/commands.md'].",
        ),
        postconditions=(
            _postcond_file_exists("design/commands.md"),
            _postcond_file_contains("design/commands.md", "pause"),
            _postcond_file_contains("design/commands.md", "resume"),
            _postcond_file_contains("design/commands.md", "abort"),
            _postcond_file_contains("design/commands.md", "note"),
            _postcond_file_contains("design/commands.md", "comment"),
            _postcond_file_contains("design/commands.md", "redirect"),
            _postcond_file_contains("design/commands.md", "review"),
            _postcond_file_contains("design/commands.md", "help"),
            _postcond_mark_complete(),
        ),
        depends_on=("fetch_commands",),
        output_path="design/commands.md",
    ))

    # --- 6. design_bridge_spec ---
    tasks.append(_task(
        "design_bridge_spec",
        "architect",
        _step(
            step="Write the MatrixBridge class interface spec.",
            inputs="Read kb/nio.md.",
            output="design/bridge.md",
            requirement="Must describe a class MatrixBridge with: "
                        "__init__(homeserver, user_id, password, room_id); "
                        "async login() — creates AsyncClient and calls login(password); "
                        "async send_agora_command(text) — calls client.room_send with "
                        "message_type='m.room.message' and content={'msgtype': 'm.text', 'body': text}; "
                        "async close() — closes the client.",
            tools="1) read_file path=kb/nio.md. "
                  "2) write_file path=design/bridge.md content=<markdown with the 4 methods>. "
                  "3) mark_complete summary='bridge spec' artifacts=['design/bridge.md'].",
        ),
        postconditions=(
            _postcond_file_exists("design/bridge.md"),
            _postcond_file_contains("design/bridge.md", "MatrixBridge"),
            _postcond_file_contains("design/bridge.md", "send_agora_command"),
            _postcond_file_contains("design/bridge.md", "room_send"),
            _postcond_mark_complete(),
        ),
        depends_on=("fetch_nio",),
        output_path="design/bridge.md",
    ))

    # --- 7. build_config ---
    tasks.append(_task(
        "build_config",
        "impl",
        _step(
            step="Write config.py with a frozen dataclass + from_env classmethod.",
            inputs="Read design/modules.md.",
            output="config.py",
            requirement="config.py must define @dataclass(frozen=True) class Config with "
                        "str fields discord_token, matrix_homeserver, matrix_user, "
                        "matrix_password, matrix_room_id, and a @classmethod from_env() "
                        "that reads os.environ for each (DISCORD_TOKEN, MATRIX_HOMESERVER, "
                        "MATRIX_USER, MATRIX_PASSWORD, MATRIX_ROOM_ID).",
            tools="1) read_file path=design/modules.md. "
                  "2) write_file path=config.py content=<python dataclass module>. "
                  "3) mark_complete summary='config' artifacts=['config.py'].",
        ),
        postconditions=(
            _postcond_file_exists("config.py"),
            _postcond_py_compiles("config.py"),
            postcond_python_imports("config.py"),
            _postcond_file_contains("config.py", "class Config"),
            _postcond_file_contains("config.py", "from_env"),
            _postcond_file_contains("config.py", "MATRIX_HOMESERVER"),
            _postcond_mark_complete(),
        ),
        depends_on=("design_modules",),
        output_path="config.py",
    ))

    # --- 8. build_bridge_skeleton ---
    tasks.append(_task(
        "build_bridge_skeleton",
        "impl",
        _step(
            step="Write matrix_bridge.py with the MatrixBridge class skeleton "
                 "(init + login + close; no send_agora_command yet).",
            inputs="Read design/bridge.md.",
            output="matrix_bridge.py",
            requirement="matrix_bridge.py must: import AsyncClient, LoginResponse from nio; "
                        "define class MatrixBridge with __init__(homeserver, user_id, "
                        "password, room_id) storing all fields plus self.client = None; "
                        "async login() that builds AsyncClient, awaits login(password), "
                        "and raises RuntimeError if the response is not LoginResponse; "
                        "async close() that awaits client.close() when client is not None. "
                        "NO send_agora_command method yet.",
            tools="1) read_file path=design/bridge.md. "
                  "2) write_file path=matrix_bridge.py content=<python module>. "
                  "3) mark_complete summary='bridge skeleton' artifacts=['matrix_bridge.py'].",
        ),
        postconditions=(
            _postcond_file_exists("matrix_bridge.py"),
            _postcond_py_compiles("matrix_bridge.py"),
            postcond_python_imports("matrix_bridge.py"),
            _postcond_file_contains("matrix_bridge.py", "class MatrixBridge"),
            _postcond_file_contains("matrix_bridge.py", "AsyncClient"),
            _postcond_file_contains("matrix_bridge.py", "LoginResponse"),
            _postcond_file_contains("matrix_bridge.py", "async def login"),
            _postcond_file_contains("matrix_bridge.py", "async def close"),
            _postcond_mark_complete(),
        ),
        depends_on=("design_bridge_spec",),
        output_path="matrix_bridge.py",
    ))

    # --- 9. build_bridge_send ---
    tasks.append(_task(
        "build_bridge_send",
        "impl",
        _step(
            step="Add the send_agora_command method to matrix_bridge.py.",
            inputs="Read matrix_bridge.py.",
            output="matrix_bridge.py",
            requirement="matrix_bridge.py must keep the skeleton AND add an async "
                        "send_agora_command(self, text: str) method that: raises "
                        "RuntimeError if self.client is None, else awaits self.client.room_send "
                        "with room_id=self.room_id, message_type='m.room.message', "
                        "content={'msgtype': 'm.text', 'body': text}.",
            tools="1) read_file path=matrix_bridge.py. "
                  "2) edit_file_insert_before path='matrix_bridge.py' anchor='async def close' "
                  "snippet=<the send_agora_command method>. "
                  "Do NOT call write_file. Do NOT re-emit existing module content. "
                  "3) mark_complete summary='send method added' artifacts=['matrix_bridge.py'].",
        ),
        postconditions=(
            _postcond_file_exists("matrix_bridge.py"),
            _postcond_py_compiles("matrix_bridge.py"),
            postcond_python_imports("matrix_bridge.py"),
            _postcond_file_contains("matrix_bridge.py", "send_agora_command"),
            _postcond_file_contains("matrix_bridge.py", "room_send"),
            _postcond_file_contains("matrix_bridge.py", "m.room.message"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_bridge_skeleton",),
        output_path="matrix_bridge.py",
    ))

    # --- 10. build_skeleton (bot.py) ---
    tasks.append(_task(
        "build_skeleton",
        "impl",
        _step(
            step="Write bot.py with the bot skeleton (no slash command handlers yet).",
            inputs="Read design/modules.md, design/commands.md, config.py, matrix_bridge.py.",
            output="bot.py",
            requirement="bot.py must: import discord and commands from discord.ext, import "
                        "Config from config, import MatrixBridge from matrix_bridge; create "
                        "bot = commands.Bot(command_prefix='!', intents=intents); declare "
                        "module-level bridge: MatrixBridge | None = None; define "
                        "@bot.event async def on_ready that reads Config.from_env(), builds "
                        "a MatrixBridge, logs in, and calls await bot.tree.sync(); under "
                        "if __name__ == '__main__' call bot.run(Config.from_env().discord_token). "
                        "NO slash command decorators yet.",
            tools="1) read_file path=design/modules.md. "
                  "2) read_file path=design/commands.md. "
                  "3) write_file path=bot.py content=<python skeleton>. "
                  "4) mark_complete summary='bot skeleton' artifacts=['bot.py'].",
        ),
        postconditions=(
            _postcond_file_exists("bot.py"),
            _postcond_py_compiles("bot.py"),
            postcond_python_imports("bot.py"),
            postcond_no_code_after_main_block("bot.py"),
            _postcond_file_contains("bot.py", "discord"),
            _postcond_file_contains("bot.py", "MatrixBridge"),
            _postcond_file_contains("bot.py", "DISCORD_TOKEN"),
            _postcond_file_contains("bot.py", "tree.sync"),
            _postcond_mark_complete(),
        ),
        depends_on=("design_modules", "design_commands_spec", "build_config", "build_bridge_send"),
        output_path="bot.py",
    ))

    # --- 11–18. build_cmd_<verb> — sequential chain (all edit bot.py) ---
    prev_dep = "build_skeleton"
    for cmd in COMMAND_MAP:
        tid = f"build_cmd_{cmd['name']}"
        tasks.append(_task(
            tid,
            "impl",
            _step(
                step=f"Add the /{cmd['name']} slash command to bot.py.",
                inputs="Read bot.py.",
                output="bot.py",
                requirement=f"bot.py must add an @bot.tree.command(name='{cmd['name']}') "
                            f"handler that calls bridge.send_agora_command with the "
                            f"appropriate '/agora {cmd['agora_verb']}' payload and replies "
                            f"to the interaction.",
                tools=f"1) read_file path=bot.py. "
                      f"2) edit_file_insert_before path='bot.py' anchor=\"if __name__\" "
                      f"snippet=<the new @bot.tree.command decorator block>. "
                      f"Do NOT call write_file. Do NOT re-emit existing bot.py content. "
                      f"3) mark_complete summary='/{cmd['name']} added' artifacts=['bot.py'].",
            ),
            postconditions=(
                _postcond_file_exists("bot.py"),
                _postcond_py_compiles("bot.py"),
                postcond_python_imports("bot.py"),
                postcond_no_code_after_main_block("bot.py"),
                _postcond_file_contains("bot.py", f"name='{cmd['name']}'"),
                _postcond_mark_complete(),
            ),
            depends_on=(prev_dep,),
            output_path="bot.py",
        ))
        prev_dep = tid

    # --- 19. write_requirements ---
    tasks.append(_task(
        "write_requirements",
        "impl",
        _step(
            step="Write requirements.txt pinning discord.py and matrix-nio.",
            inputs="None.",
            output="requirements.txt",
            requirement="requirements.txt must contain two lines: discord.py>=2.3 and "
                        "matrix-nio>=0.20 (no imports, no comments).",
            tools="1) write_file path=requirements.txt content='discord.py>=2.3\\nmatrix-nio>=0.20\\n'. "
                  "2) mark_complete summary='requirements' artifacts=['requirements.txt'].",
        ),
        postconditions=(
            _postcond_file_exists("requirements.txt"),
            _postcond_file_contains("requirements.txt", "discord.py"),
            _postcond_file_contains("requirements.txt", "matrix-nio"),
            postcond_requirements_parse("requirements.txt"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_skeleton",),
        output_path="requirements.txt",
    ))

    # --- 20. write_readme ---
    tasks.append(_task(
        "write_readme",
        "impl",
        _step(
            step="Write a short README.md explaining how to run the bot.",
            inputs="Read bot.py (every slash command is now registered).",
            output="README.md",
            requirement="README.md must mention the env vars DISCORD_TOKEN, "
                        "MATRIX_HOMESERVER, MATRIX_USER, MATRIX_PASSWORD, "
                        "MATRIX_ROOM_ID and `python bot.py`. Describe usage of whatever "
                        "slash commands exist — do NOT reference commands that aren't "
                        "declared in bot.py.",
            tools="1) read_file path=bot.py. "
                  "2) write_file path=README.md content=<short markdown>. "
                  "3) mark_complete summary='readme' artifacts=['README.md'].",
        ),
        postconditions=(
            _postcond_file_exists("README.md"),
            _postcond_file_contains("README.md", "DISCORD_TOKEN"),
            _postcond_file_contains("README.md", "MATRIX_HOMESERVER"),
            postcond_readme_only_references_existing_commands(),
            _postcond_mark_complete(),
        ),
        depends_on=("build_cmd_help",),
        output_path="README.md",
    ))

    # --- 21. write_tests_bridge ---
    tasks.append(_task(
        "write_tests_bridge",
        "tester",
        _step(
            step="Write test_matrix_bridge.py with pytest tests for MatrixBridge.",
            inputs="Read matrix_bridge.py.",
            output="test_matrix_bridge.py",
            requirement="test_matrix_bridge.py must contain at least two "
                        "`def test_` functions. It must NOT hit a real network: mock "
                        "self.client via unittest.mock.AsyncMock. One test verifies "
                        "send_agora_command raises RuntimeError when client is None; "
                        "another test verifies it calls client.room_send with the correct "
                        "room_id and content body.",
            tools="1) read_file path=matrix_bridge.py. "
                  "2) write_file path=test_matrix_bridge.py content=<pytest code with AsyncMock>. "
                  "3) mark_complete summary='bridge tests' artifacts=['test_matrix_bridge.py'].",
        ),
        postconditions=(
            _postcond_file_exists("test_matrix_bridge.py"),
            _postcond_py_compiles("test_matrix_bridge.py"),
            _postcond_file_contains("test_matrix_bridge.py", "def test_"),
            _postcond_file_contains("test_matrix_bridge.py", "AsyncMock"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_bridge_send",),
        output_path="test_matrix_bridge.py",
    ))

    # --- 22. write_tests_commands ---
    tasks.append(_task(
        "write_tests_commands",
        "tester",
        _step(
            step="Write test_bot_commands.py — pytest tests for every slash command.",
            inputs="Read bot.py and matrix_bridge.py.",
            output="test_bot_commands.py",
            requirement="test_bot_commands.py must: introspect bot.bot.tree.get_commands() "
                        "to assert that all 8 command names (pause, resume, abort, note, "
                        "comment, redirect, review, help) are registered; and verify that "
                        "invoking the /pause command's .callback(...) sends '/agora pause' "
                        "via a mocked bridge. Use AsyncMock for both the bridge and the "
                        "Interaction. Tests must NOT require a real Discord token or real "
                        "Matrix server.",
            tools="1) read_file path=bot.py. 2) read_file path=matrix_bridge.py. "
                  "3) write_file path=test_bot_commands.py content=<pytest code with mocks>. "
                  "4) mark_complete summary='command tests' artifacts=['test_bot_commands.py'].",
        ),
        postconditions=(
            _postcond_file_exists("test_bot_commands.py"),
            _postcond_py_compiles("test_bot_commands.py"),
            _postcond_file_contains("test_bot_commands.py", "def test_"),
            _postcond_file_contains("test_bot_commands.py", "AsyncMock"),
            postcond_pytest_passes("."),
            _postcond_mark_complete(),
        ),
        depends_on=("build_cmd_help", "write_requirements", "write_tests_bridge"),
        output_path="test_bot_commands.py",
    ))

    # --- 23. integration_check (terminal gate) ---
    tasks.append(_task(
        "integration_check",
        "tester",
        _step(
            step="Confirm the repo is complete. DO NOT modify any files.",
            inputs="The entire workspace.",
            output="(no new file — this task only verifies)",
            requirement="Just call mark_complete. Gate postconditions verify config.py "
                        "imports, matrix_bridge.py imports, bot.py imports, bot.py "
                        "declares all eight commands, bot.py calls tree.sync, "
                        "requirements.txt parses, pytest passes, and README.md only "
                        "references declared commands.",
            tools="1) mark_complete summary='integration OK' artifacts=[].",
        ),
        postconditions=(
            postcond_python_imports("config.py"),
            postcond_python_imports("matrix_bridge.py"),
            postcond_python_imports("bot.py"),
            postcond_no_code_after_main_block("bot.py"),
            postcond_bot_calls_tree_sync("bot.py"),
            _postcond_all_commands_registered(),
            postcond_requirements_parse("requirements.txt"),
            postcond_pytest_passes("."),
            postcond_readme_only_references_existing_commands(),
            _postcond_mark_complete(),
        ),
        depends_on=(
            "build_cmd_help", "write_requirements", "write_readme",
            "write_tests_bridge", "write_tests_commands",
        ),
    ))

    return tasks


# ---------------------------------------------------------------------- staging

def build_staged_tasks(tasks: list[Task]) -> dict[str, StagedTask]:
    """Stage every task where a literal template beats free-form emission.

    The design/fetch tasks stay one-shot (they write prose, not code that
    needs line-precise templates). Everything touching Python source is
    staged.
    """
    by_id = {t.id: t for t in tasks}
    staged: dict[str, StagedTask] = {}

    # ----- requirements.txt — literal content, one write_file call -----
    if "write_requirements" in by_id:
        staged["write_requirements"] = StagedTask(
            task=by_id["write_requirements"],
            stages=[
                Stage(
                    name="write",
                    instruction=(
                        "Write the file `requirements.txt` with EXACTLY this content "
                        "(two lines, no comments, no imports):\n"
                        "discord.py>=2.3\n"
                        "matrix-nio>=0.20\n\n"
                        "Call write_file path='requirements.txt' "
                        "content='discord.py>=2.3\\nmatrix-nio>=0.20\\n'."
                    ),
                    max_iterations=4,
                ),
            ],
        )

    # ----- config.py — literal dataclass template -----
    if "build_config" in by_id:
        staged["build_config"] = StagedTask(
            task=by_id["build_config"],
            stages=[
                Stage(
                    name="write_config",
                    instruction=(
                        "Write `config.py` with this EXACT content:\n\n"
                        "```python\n"
                        "import os\n"
                        "from dataclasses import dataclass\n"
                        "\n"
                        "\n"
                        "@dataclass(frozen=True)\n"
                        "class Config:\n"
                        "    discord_token: str\n"
                        "    matrix_homeserver: str\n"
                        "    matrix_user: str\n"
                        "    matrix_password: str\n"
                        "    matrix_room_id: str\n"
                        "\n"
                        "    @classmethod\n"
                        "    def from_env(cls) -> 'Config':\n"
                        "        return cls(\n"
                        "            discord_token=os.environ['DISCORD_TOKEN'],\n"
                        "            matrix_homeserver=os.environ['MATRIX_HOMESERVER'],\n"
                        "            matrix_user=os.environ['MATRIX_USER'],\n"
                        "            matrix_password=os.environ['MATRIX_PASSWORD'],\n"
                        "            matrix_room_id=os.environ['MATRIX_ROOM_ID'],\n"
                        "        )\n"
                        "```\n\n"
                        "Call write_file path='config.py' content=<the code above verbatim>."
                    ),
                    max_iterations=5,
                ),
            ],
        )

    # ----- matrix_bridge.py skeleton — literal template -----
    if "build_bridge_skeleton" in by_id:
        staged["build_bridge_skeleton"] = StagedTask(
            task=by_id["build_bridge_skeleton"],
            stages=[
                Stage(
                    name="write_bridge",
                    instruction=(
                        "Write `matrix_bridge.py` with this EXACT content:\n\n"
                        "```python\n"
                        "from nio import AsyncClient, LoginResponse\n"
                        "\n"
                        "\n"
                        "class MatrixBridge:\n"
                        "    def __init__(self, homeserver: str, user_id: str, "
                        "password: str, room_id: str) -> None:\n"
                        "        self.homeserver = homeserver\n"
                        "        self.user_id = user_id\n"
                        "        self.password = password\n"
                        "        self.room_id = room_id\n"
                        "        self.client: AsyncClient | None = None\n"
                        "\n"
                        "    async def login(self) -> None:\n"
                        "        self.client = AsyncClient(self.homeserver, self.user_id)\n"
                        "        resp = await self.client.login(self.password)\n"
                        "        if not isinstance(resp, LoginResponse):\n"
                        "            raise RuntimeError(f'matrix login failed: {resp}')\n"
                        "\n"
                        "    async def close(self) -> None:\n"
                        "        if self.client is not None:\n"
                        "            await self.client.close()\n"
                        "```\n\n"
                        "Do NOT add send_agora_command yet — that's the next task. "
                        "Call write_file path='matrix_bridge.py' content=<the code above verbatim>."
                    ),
                    context_files=("design/bridge.md",),
                    max_iterations=5,
                ),
            ],
        )

    # ----- add send_agora_command via insert_before `async def close` -----
    if "build_bridge_send" in by_id:
        staged["build_bridge_send"] = StagedTask(
            task=by_id["build_bridge_send"],
            stages=[
                Stage(
                    name="add_send",
                    instruction=(
                        "Add the send_agora_command method to matrix_bridge.py. Call ONE tool:\n\n"
                        "edit_file_insert_before(\n"
                        "    path='matrix_bridge.py',\n"
                        "    anchor=\"async def close\",\n"
                        "    snippet=\"\"\"\n"
                        "    async def send_agora_command(self, text: str) -> None:\n"
                        "        if self.client is None:\n"
                        "            raise RuntimeError('not logged in')\n"
                        "        await self.client.room_send(\n"
                        "            self.room_id,\n"
                        "            message_type='m.room.message',\n"
                        "            content={'msgtype': 'm.text', 'body': text},\n"
                        "        )\n"
                        "\n"
                        "\"\"\",\n"
                        ")\n\n"
                        "Do NOT call write_file. Do NOT re-emit matrix_bridge.py. "
                        "The anchor 'async def close' already appears in the file. "
                        "Note the snippet starts with 4 leading spaces on each line — "
                        "that's the class-body indentation required so the new method "
                        "lands inside class MatrixBridge."
                    ),
                    context_files=("matrix_bridge.py",),
                    max_iterations=6,
                ),
            ],
        )

    # ----- design/bridge.md — literal markdown (skips the 64KB nio.md read) -----
    if "design_bridge_spec" in by_id:
        staged["design_bridge_spec"] = StagedTask(
            task=by_id["design_bridge_spec"],
            stages=[
                Stage(
                    name="write_bridge_md",
                    instruction=(
                        "Write `design/bridge.md` with this EXACT markdown content:\n\n"
                        "```markdown\n"
                        "# MatrixBridge — interface spec\n"
                        "\n"
                        "A thin wrapper over `nio.AsyncClient` that lets the Discord bot "
                        "forward `/agora` command text into a Matrix project room.\n"
                        "\n"
                        "## Class: MatrixBridge\n"
                        "\n"
                        "- `__init__(homeserver: str, user_id: str, password: str, "
                        "room_id: str)` — stores all fields plus `self.client = None`.\n"
                        "- `async login() -> None` — creates `AsyncClient(homeserver, "
                        "user_id)` and awaits `client.login(password)`. Raises "
                        "`RuntimeError` if the response is not a `LoginResponse`.\n"
                        "- `async send_agora_command(text: str) -> None` — raises "
                        "`RuntimeError` if `self.client is None`, else awaits "
                        "`client.room_send(room_id, message_type='m.room.message', "
                        "content={'msgtype': 'm.text', 'body': text})`.\n"
                        "- `async close() -> None` — awaits `client.close()` when client "
                        "is not None.\n"
                        "```\n\n"
                        "Do NOT call read_file on kb/nio.md — this spec is already "
                        "fully prescribed. "
                        "Call write_file path='design/bridge.md' content=<the markdown above "
                        "verbatim>."
                    ),
                    max_iterations=4,
                ),
            ],
        )

    # ----- bot.py skeleton — literal template -----
    if "build_skeleton" in by_id:
        staged["build_skeleton"] = StagedTask(
            task=by_id["build_skeleton"],
            stages=[
                Stage(
                    name="write_bot_skeleton",
                    instruction=(
                        "Write `bot.py` with this EXACT structure:\n\n"
                        "```python\n"
                        "import discord\n"
                        "from discord.ext import commands\n"
                        "\n"
                        "from config import Config\n"
                        "from matrix_bridge import MatrixBridge\n"
                        "\n"
                        "DISCORD_TOKEN = 'set-via-Config.from_env'  # env var name is "
                        "DISCORD_TOKEN\n"
                        "\n"
                        "intents = discord.Intents.default()\n"
                        "intents.message_content = True\n"
                        "bot = commands.Bot(command_prefix='!', intents=intents)\n"
                        "\n"
                        "bridge: MatrixBridge | None = None\n"
                        "\n"
                        "\n"
                        "@bot.event\n"
                        "async def on_ready():\n"
                        "    global bridge\n"
                        "    config = Config.from_env()\n"
                        "    bridge = MatrixBridge(\n"
                        "        homeserver=config.matrix_homeserver,\n"
                        "        user_id=config.matrix_user,\n"
                        "        password=config.matrix_password,\n"
                        "        room_id=config.matrix_room_id,\n"
                        "    )\n"
                        "    await bridge.login()\n"
                        "    await bot.tree.sync()\n"
                        "    print(f'{bot.user} ready')\n"
                        "\n"
                        "\n"
                        "if __name__ == '__main__':\n"
                        "    bot.run(Config.from_env().discord_token)\n"
                        "```\n\n"
                        "Use discord.Intents (NOT commands.Intents). command_prefix='!' "
                        "is required. Do NOT add slash command decorators yet — those "
                        "come in later tasks. The module-level `bridge: MatrixBridge | "
                        "None = None` is important: the on_ready hook mutates it via "
                        "`global bridge`. "
                        "Call write_file path='bot.py' content=<the code above verbatim>."
                    ),
                    context_files=("design/modules.md", "design/commands.md"),
                    max_iterations=6,
                ),
            ],
        )

    # ----- build_cmd_<verb> × 8 — single edit_file_insert_before per task -----
    for cmd in COMMAND_MAP:
        tid = f"build_cmd_{cmd['name']}"
        if tid not in by_id:
            continue
        snippet = _handler_snippet(cmd)
        # The snippet is embedded verbatim in the instruction; use triple quotes
        # for clarity and so the model can copy-paste it exactly.
        staged[tid] = StagedTask(
            task=by_id[tid],
            stages=[
                Stage(
                    name=f"add_{cmd['name']}",
                    instruction=(
                        f"Add the /{cmd['name']} slash command to bot.py. Call ONE tool:\n\n"
                        f"edit_file_insert_before(\n"
                        f"    path='bot.py',\n"
                        f"    anchor=\"if __name__\",\n"
                        f"    snippet=\"\"\"\n{snippet}\n\"\"\",\n"
                        f")\n\n"
                        f"Do NOT call write_file. Do NOT call edit_file_append — "
                        f"handlers appended after `if __name__` won't register at "
                        f"runtime (bot.run blocks). Always insert BEFORE `if __name__`. "
                        f"The function is named `{cmd['name']}_cmd` (suffix avoids a "
                        f"name clash if {cmd['name']} is also a Python builtin or local)."
                    ),
                    context_files=("bot.py",),
                    max_iterations=5,
                ),
            ],
        )

    # ----- test_matrix_bridge.py — literal pytest template -----
    if "write_tests_bridge" in by_id:
        staged["write_tests_bridge"] = StagedTask(
            task=by_id["write_tests_bridge"],
            stages=[
                Stage(
                    name="write_bridge_tests",
                    instruction=(
                        "Write `test_matrix_bridge.py` with this EXACT template:\n\n"
                        "```python\n"
                        "import asyncio\n"
                        "from unittest.mock import AsyncMock\n"
                        "\n"
                        "import pytest\n"
                        "\n"
                        "from matrix_bridge import MatrixBridge\n"
                        "\n"
                        "\n"
                        "def _bridge() -> MatrixBridge:\n"
                        "    return MatrixBridge('http://hs', '@u:hs', 'pw', '!r:hs')\n"
                        "\n"
                        "\n"
                        "def test_send_before_login_raises():\n"
                        "    bridge = _bridge()\n"
                        "    with pytest.raises(RuntimeError):\n"
                        "        asyncio.run(bridge.send_agora_command('/agora pause'))\n"
                        "\n"
                        "\n"
                        "def test_send_calls_room_send_with_body():\n"
                        "    bridge = _bridge()\n"
                        "    bridge.client = AsyncMock()\n"
                        "    asyncio.run(bridge.send_agora_command('/agora pause'))\n"
                        "    bridge.client.room_send.assert_called_once()\n"
                        "    args, kwargs = bridge.client.room_send.call_args\n"
                        "    assert (args and args[0] == '!r:hs') or kwargs.get('room_id') == '!r:hs'\n"
                        "    content = kwargs.get('content') or (args[2] if len(args) > 2 else None)\n"
                        "    assert content == {'msgtype': 'm.text', 'body': '/agora pause'}\n"
                        "```\n\n"
                        "Call write_file path='test_matrix_bridge.py' "
                        "content=<the code above verbatim>."
                    ),
                    context_files=("matrix_bridge.py",),
                    max_iterations=6,
                ),
            ],
        )

    # ----- test_bot_commands.py — literal pytest template -----
    if "write_tests_commands" in by_id:
        staged["write_tests_commands"] = StagedTask(
            task=by_id["write_tests_commands"],
            stages=[
                Stage(
                    name="write_command_tests",
                    instruction=(
                        "Write `test_bot_commands.py` with this EXACT template:\n\n"
                        "```python\n"
                        "import asyncio\n"
                        "from unittest.mock import AsyncMock\n"
                        "\n"
                        "import bot as bot_module\n"
                        "\n"
                        "EXPECTED = {'pause', 'resume', 'abort', 'note', "
                        "'comment', 'redirect', 'review', 'help'}\n"
                        "\n"
                        "\n"
                        "def _names() -> set[str]:\n"
                        "    return {c.name for c in bot_module.bot.tree.get_commands()}\n"
                        "\n"
                        "\n"
                        "def test_all_commands_registered():\n"
                        "    assert EXPECTED.issubset(_names())\n"
                        "\n"
                        "\n"
                        "def _cmd(name):\n"
                        "    return next(c for c in bot_module.bot.tree.get_commands() "
                        "if c.name == name)\n"
                        "\n"
                        "\n"
                        "def test_pause_sends_agora_pause():\n"
                        "    bot_module.bridge = AsyncMock()\n"
                        "    interaction = AsyncMock()\n"
                        "    asyncio.run(_cmd('pause').callback(interaction))\n"
                        "    bot_module.bridge.send_agora_command.assert_called_once_with("
                        "'/agora pause')\n"
                        "```\n\n"
                        "Note: discord's @bot.tree.command wraps the handler in a Command "
                        "object; call `.callback(...)` to invoke the underlying coroutine. "
                        "Call write_file path='test_bot_commands.py' "
                        "content=<the code above verbatim>."
                    ),
                    context_files=("bot.py",),
                    max_iterations=7,
                ),
            ],
        )

    return staged


# ---------------------------------------------------------------------- main

async def _preflight() -> None:
    print(f"[*] VRAM check for {LLM_MODEL}...")
    check = await check_model_fits(LLM_MODEL, base_url=OLLAMA_BASE_URL)
    print(f"  {check.reason}")
    raise_if_wont_fit(check, LLM_MODEL)


async def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    REPO_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    KB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
        print(f"[*] Auto-inviting {OBSERVER_USER} to every created room")

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
        repo_root=str(REPO_ROOT_DIR),
        knowledge_cache_dir=str(KB_CACHE_DIR),
        ollama_base_url=OLLAMA_BASE_URL,
        skip_warmup=False,
        warmup_deadline=600.0,
        review_timeout_seconds=REVIEW_TIMEOUT,
        enable_web_fetch=True,
        fetch_timeout_seconds=30.0,
        fetch_max_bytes=1_048_576,
        fetch_max_text_bytes=65_536,
        auto_hooks_enabled=True,
    )

    print("[*] Running project 'discord-bot-full' (observer enabled)")
    print("   open Element as @fabs:agora.local to watch and vote on the REVIEW poll")
    print(f"   review_timeout_seconds={REVIEW_TIMEOUT}")
    print(f"   max_task_retries={MAX_TASK_RETRIES}")
    print()
    try:
        tasks = build_tasks()
        staged = build_staged_tasks(tasks)
        result = await orchestrator.run_project(
            "discord-bot-full",
            build_agents(),
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
    print(f"Tokens: in={result.total_tokens.get('input_tokens', 0)}, "
          f"out={result.total_tokens.get('output_tokens', 0)}")
    for r in result.task_results:
        mark = "OK" if r.success else "FAIL"
        print(f"  [{mark}] {r.task_id}: {r.iterations} iter  -> {r.output[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
