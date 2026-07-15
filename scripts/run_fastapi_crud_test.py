"""Autonomous test run: instruct Agora to build a FastAPI CRUD service.

Same ~13-task shape as ``run_discord_bot_test.py`` but in a new domain —
stress-tests whether the Agora scaffolding generalizes beyond the Discord-bot
template it was tuned on.

Target project: a FastAPI service with five endpoints over an in-memory Item
store (no database), plus tests via ``fastapi.testclient.TestClient``.

Run with:

    .venv/Scripts/python.exe scripts/run_fastapi_crud_test.py
"""

from __future__ import annotations

import asyncio
import io
import sys
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
logging.getLogger("nio").setLevel(logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.config import get_settings, require_secret
from agora.core.agent import AgentConfig
from agora.core.contract import Specification
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.llm_adapter import create_llm_adapter
from agora.fleet.orchestrator import Orchestrator
from agora.fleet.runtime_postconditions import (
    postcond_no_code_after_main_block,
    postcond_pytest_passes,
    postcond_python_imports,
    postcond_requirements_parse,
)
from agora.fleet.stage_runner import Stage, StagedTask
from agora.fleet.vram import check_model_fits, raise_if_wont_fit
from agora.matrix.client import AgoraMatrixClient
from agora.matrix.room_manager import RoomManager

# Config comes from one source: Settings (env is read only in config.py). This
# script is a composition root — it reads Settings once and injects typed values.
_settings = get_settings()
HOMESERVER = _settings.matrix_homeserver
SERVER_NAME = _settings.matrix_server_name
SYSTEM_USER = _settings.matrix_user_id
SYSTEM_PASSWORD = _settings.matrix_password
OBSERVER_USER = _settings.observer_user
OLLAMA_BASE_URL = _settings.ollama_base_url
LLM_MODEL = _settings.llm_model
REVIEW_TIMEOUT = _settings.review_timeout_seconds
MAX_PARALLEL = _settings.max_parallel_agents
MAX_TASK_RETRIES = _settings.max_task_retries
WORK_DIR = REPO_ROOT / "workspace"
REPO_ROOT_DIR = WORK_DIR
KB_CACHE_DIR = WORK_DIR / ".knowledge"


# These predicate helpers were previously defined inline in this script. They
# now live in agora.plan.predicate_registry under short names (``file_exists``,
# ``file_contains``, ``mark_complete``, ``py_compiles``) so YAML plans can
# reference them by name. The aliases below preserve the ``_postcond_*``
# identifiers used throughout this file so the runner keeps working byte-for-
# byte identically to the pre-lift version — Specification.fingerprint is
# stable because predicate names match (see naming-comments in the registry).
from agora.plan.predicate_registry import (
    postcond_file_contains as _postcond_file_contains,
)
from agora.plan.predicate_registry import (
    postcond_file_exists as _postcond_file_exists,
)
from agora.plan.predicate_registry import (
    postcond_mark_complete as _postcond_mark_complete,
)
from agora.plan.predicate_registry import (
    postcond_py_compiles as _postcond_py_compiles,
)

ARCHITECT_INSTRUCTIONS = """\
You are the ARCHITECT. Follow each task's description literally — one concrete
action per task. Always finish with `mark_complete(summary=..., artifacts=[...])`.
Prefer calling a single tool at a time.
"""

IMPLEMENTER_INSTRUCTIONS = """\
You are the IMPLEMENTER. Focus on ONE thing per task: write the requested file
with `write_file` (new files) or `edit_file_*` (modifying existing files). The
framework handles validation, git commits, and task completion automatically.

If a previous tool call returned a validation error on the next turn (syntax,
import, or missing name), read the file and re-write it with the fix.
"""

TESTER_INSTRUCTIONS = """\
You are the TESTER. Focus on ONE thing per task: write the requested test
file with `write_file`. The framework handles validation and completion
automatically.
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


def build_tasks() -> list[Task]:
    tasks: list[Task] = []

    # --- 1. fetch_intro ---
    tasks.append(_task(
        "fetch_intro",
        "architect",
        _step(
            step="Fetch the FastAPI first-steps tutorial and save it locally.",
            inputs="URL https://fastapi.tiangolo.com/tutorial/first-steps/",
            output="kb/intro.md",
            requirement="kb/intro.md must exist and contain real FastAPI tutorial text.",
            tools="1) fetch_url url=https://fastapi.tiangolo.com/tutorial/first-steps/ "
                  "save_as=kb/intro.md (writes atomically — do NOT call write_file). "
                  "2) mark_complete summary='fetched intro' artifacts=['kb/intro.md'].",
        ),
        postconditions=(
            _postcond_file_exists("kb/intro.md"),
            _postcond_mark_complete(),
        ),
        output_path="kb/intro.md",
    ))

    # --- 2. fetch_body ---
    tasks.append(_task(
        "fetch_body",
        "architect",
        _step(
            step="Fetch the FastAPI request-body tutorial and save it locally.",
            inputs="URL https://fastapi.tiangolo.com/tutorial/body/",
            output="kb/body.md",
            requirement="kb/body.md must exist and mention pydantic / BaseModel.",
            tools="1) fetch_url url=https://fastapi.tiangolo.com/tutorial/body/ "
                  "save_as=kb/body.md (writes atomically — do NOT call write_file). "
                  "2) mark_complete summary='fetched body tutorial' artifacts=['kb/body.md'].",
        ),
        postconditions=(
            _postcond_file_exists("kb/body.md"),
            _postcond_mark_complete(),
        ),
        output_path="kb/body.md",
    ))

    # --- 3. design_modules ---
    tasks.append(_task(
        "design_modules",
        "architect",
        _step(
            step="Write a short module layout for the FastAPI CRUD project.",
            inputs="Read kb/intro.md and kb/body.md.",
            output="design/modules.md",
            requirement="Must list files: app.py (FastAPI app), requirements.txt, "
                        "README.md, test_app.py. One line per file, with a "
                        "1-sentence purpose.",
            tools="1) read_file path=kb/intro.md. 2) read_file path=kb/body.md. "
                  "3) write_file path=design/modules.md content=<markdown with bulleted file list>. "
                  "4) mark_complete summary='modules' artifacts=['design/modules.md'].",
        ),
        postconditions=(
            _postcond_file_exists("design/modules.md"),
            _postcond_file_contains("design/modules.md", "app.py"),
            _postcond_mark_complete(),
        ),
        depends_on=("fetch_intro", "fetch_body"),
        output_path="design/modules.md",
    ))

    # --- 4. design_endpoints ---
    tasks.append(_task(
        "design_endpoints",
        "architect",
        _step(
            step="Write signatures for the five CRUD endpoints over an Item resource.",
            inputs="Read kb/body.md.",
            output="design/endpoints.md",
            requirement="Must include five endpoint signatures: "
                        "POST /items (create), GET /items (list), GET /items/{item_id} (read), "
                        "PUT /items/{item_id} (update), DELETE /items/{item_id} (delete). "
                        "Item is a pydantic BaseModel with fields: name:str, price:float.",
            tools="1) read_file path=kb/body.md. "
                  "2) write_file path=design/endpoints.md content=<markdown listing the five endpoints>. "
                  "3) mark_complete summary='endpoints' artifacts=['design/endpoints.md'].",
        ),
        postconditions=(
            _postcond_file_exists("design/endpoints.md"),
            _postcond_file_contains("design/endpoints.md", "POST"),
            _postcond_file_contains("design/endpoints.md", "GET"),
            _postcond_file_contains("design/endpoints.md", "PUT"),
            _postcond_file_contains("design/endpoints.md", "DELETE"),
            _postcond_mark_complete(),
        ),
        depends_on=("fetch_intro", "fetch_body"),
        output_path="design/endpoints.md",
    ))

    # --- 5. build_skeleton ---
    tasks.append(_task(
        "build_skeleton",
        "impl",
        _step(
            step="Write app.py with FastAPI app + Item model + in-memory store (no endpoints yet).",
            inputs="Read design/modules.md and design/endpoints.md.",
            output="app.py",
            requirement="app.py must: import FastAPI and BaseModel, define "
                        "class Item(BaseModel) with fields name:str and price:float, "
                        "create app = FastAPI(), define items: dict[int, Item] = {} and "
                        "next_id: int = 1, and under if __name__ == '__main__' call "
                        "uvicorn.run(app). NO endpoint handlers yet.",
            tools="1) read_file path=design/modules.md. "
                  "2) read_file path=design/endpoints.md. "
                  "3) write_file path=app.py content=<python skeleton>. "
                  "4) mark_complete summary='skeleton' artifacts=['app.py'].",
        ),
        postconditions=(
            _postcond_file_exists("app.py"),
            _postcond_py_compiles("app.py"),
            postcond_python_imports("app.py"),
            postcond_no_code_after_main_block("app.py"),
            _postcond_file_contains("app.py", "FastAPI"),
            _postcond_file_contains("app.py", "BaseModel"),
            _postcond_mark_complete(),
        ),
        depends_on=("design_modules", "design_endpoints"),
        output_path="app.py",
    ))

    # --- 6. build_create ---
    tasks.append(_task(
        "build_create",
        "impl",
        _step(
            step="Add the POST /items endpoint to app.py.",
            inputs="Read app.py.",
            output="app.py",
            requirement="app.py must keep skeleton AND add an @app.post('/items') "
                        "handler that stores the item in the dict, assigns it the "
                        "next id (incrementing next_id), and returns {'id': id, "
                        "'item': item}.",
            tools="1) read_file path=app.py. "
                  "2) edit_file_insert_before path='app.py' anchor=\"if __name__\" "
                  "snippet=<the new @app.post decorator block>. "
                  "Do NOT call write_file. Do NOT re-emit existing app.py content. "
                  "3) mark_complete summary='/items POST added' artifacts=['app.py'].",
        ),
        postconditions=(
            _postcond_file_exists("app.py"),
            _postcond_py_compiles("app.py"),
            postcond_python_imports("app.py"),
            postcond_no_code_after_main_block("app.py"),
            _postcond_file_contains("app.py", "@app.post"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_skeleton",),
        output_path="app.py",
    ))

    # --- 7. build_list ---
    tasks.append(_task(
        "build_list",
        "impl",
        _step(
            step="Add GET /items (list all) and GET /items/{item_id} (read one) to app.py.",
            inputs="Read app.py.",
            output="app.py",
            requirement="app.py must add TWO @app.get handlers: "
                        "@app.get('/items') returning the items dict, "
                        "and @app.get('/items/{item_id}') that looks up the id and "
                        "returns the item (or raises HTTPException 404).",
            tools="1) read_file path=app.py. "
                  "2) edit_file_insert_before path='app.py' anchor=\"if __name__\" "
                  "snippet=<both @app.get decorator blocks + an import for HTTPException>. "
                  "Do NOT call write_file. Do NOT use edit_file_append — handlers "
                  "appended after `if __name__` won't register at runtime. "
                  "3) mark_complete summary='GET handlers added' artifacts=['app.py'].",
        ),
        postconditions=(
            _postcond_file_exists("app.py"),
            _postcond_py_compiles("app.py"),
            postcond_python_imports("app.py"),
            postcond_no_code_after_main_block("app.py"),
            _postcond_file_contains("app.py", "@app.get"),
            _postcond_file_contains("app.py", "HTTPException"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_create",),
        output_path="app.py",
    ))

    # --- 8. build_update ---
    tasks.append(_task(
        "build_update",
        "impl",
        _step(
            step="Add PUT /items/{item_id} (update) to app.py.",
            inputs="Read app.py.",
            output="app.py",
            requirement="app.py must add an @app.put('/items/{item_id}') handler "
                        "that replaces the item at that id. If the id is unknown, "
                        "raise HTTPException status_code=404.",
            tools="1) read_file path=app.py. "
                  "2) edit_file_insert_before path='app.py' anchor=\"if __name__\" "
                  "snippet=<the @app.put decorator block>. "
                  "Do NOT call write_file. Do NOT use edit_file_append. "
                  "3) mark_complete summary='PUT added' artifacts=['app.py'].",
        ),
        postconditions=(
            _postcond_file_exists("app.py"),
            _postcond_py_compiles("app.py"),
            postcond_python_imports("app.py"),
            postcond_no_code_after_main_block("app.py"),
            _postcond_file_contains("app.py", "@app.put"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_list",),
        output_path="app.py",
    ))

    # --- 9. build_delete ---
    tasks.append(_task(
        "build_delete",
        "impl",
        _step(
            step="Add DELETE /items/{item_id} to app.py.",
            inputs="Read app.py.",
            output="app.py",
            requirement="app.py must add an @app.delete('/items/{item_id}') "
                        "handler that deletes the item and returns {'deleted': "
                        "item_id}. HTTPException 404 on unknown id.",
            tools="1) read_file path=app.py. "
                  "2) edit_file_insert_before path='app.py' anchor=\"if __name__\" "
                  "snippet=<the @app.delete decorator block>. "
                  "Do NOT call write_file. Do NOT use edit_file_append. "
                  "3) mark_complete summary='DELETE added' artifacts=['app.py'].",
        ),
        postconditions=(
            _postcond_file_exists("app.py"),
            _postcond_py_compiles("app.py"),
            postcond_python_imports("app.py"),
            postcond_no_code_after_main_block("app.py"),
            _postcond_file_contains("app.py", "@app.delete"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_update",),
        output_path="app.py",
    ))

    # --- 10. write_requirements ---
    tasks.append(_task(
        "write_requirements",
        "impl",
        _step(
            step="Write requirements.txt with FastAPI + uvicorn + pydantic + httpx pinned.",
            inputs="None.",
            output="requirements.txt",
            requirement="requirements.txt must list these packages, one per line: "
                        "fastapi>=0.110, uvicorn[standard]>=0.27, pydantic>=2.0, "
                        "httpx>=0.25. Every line must be a valid PEP 508 spec — "
                        "NO import statements, NO free prose.",
            tools="1) write_file path=requirements.txt content="
                  "'fastapi>=0.110\\nuvicorn[standard]>=0.27\\npydantic>=2.0\\nhttpx>=0.25\\n'. "
                  "2) mark_complete summary='requirements' artifacts=['requirements.txt'].",
        ),
        postconditions=(
            _postcond_file_exists("requirements.txt"),
            _postcond_file_contains("requirements.txt", "fastapi"),
            postcond_requirements_parse("requirements.txt"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_skeleton",),
        output_path="requirements.txt",
    ))

    # --- 11. write_readme ---
    tasks.append(_task(
        "write_readme",
        "impl",
        _step(
            step="Write a short README.md explaining how to run the FastAPI service.",
            inputs="Read app.py.",
            output="README.md",
            requirement="README.md must mention `uvicorn app:app --reload` as the "
                        "run command and list the five endpoints (POST /items, "
                        "GET /items, GET /items/{item_id}, PUT /items/{item_id}, "
                        "DELETE /items/{item_id}).",
            tools="1) read_file path=app.py. "
                  "2) write_file path=README.md content=<short markdown>. "
                  "3) mark_complete summary='readme' artifacts=['README.md'].",
        ),
        postconditions=(
            _postcond_file_exists("README.md"),
            _postcond_file_contains("README.md", "uvicorn"),
            _postcond_file_contains("README.md", "/items"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_skeleton",),
        output_path="README.md",
    ))

    # --- 12. write_tests ---
    tasks.append(_task(
        "write_tests",
        "tester",
        _step(
            step="Write test_app.py with pytest-style tests using FastAPI TestClient.",
            inputs="Read app.py.",
            output="test_app.py",
            requirement="test_app.py must contain at least two `def test_` functions "
                        "using fastapi.testclient.TestClient against the app. Use "
                        "this exact template:\n\n"
                        "```python\n"
                        "from fastapi.testclient import TestClient\n"
                        "import app as app_module\n"
                        "\n"
                        "client = TestClient(app_module.app)\n"
                        "\n"
                        "def test_create_and_list():\n"
                        "    app_module.items.clear()\n"
                        "    app_module.next_id = 1\n"
                        "    resp = client.post('/items', json={'name': 'x', 'price': 1.0})\n"
                        "    assert resp.status_code == 200\n"
                        "    resp = client.get('/items')\n"
                        "    assert resp.status_code == 200\n"
                        "    assert len(resp.json()) == 1\n"
                        "\n"
                        "def test_404_on_missing():\n"
                        "    app_module.items.clear()\n"
                        "    resp = client.get('/items/999')\n"
                        "    assert resp.status_code == 404\n"
                        "```\n\n"
                        "Pytest MUST pass. Do NOT require a database.",
            tools="1) read_file path=app.py. "
                  "2) write_file path=test_app.py content=<the code above verbatim>. "
                  "3) mark_complete summary='tests' artifacts=['test_app.py'].",
        ),
        postconditions=(
            _postcond_file_exists("test_app.py"),
            _postcond_py_compiles("test_app.py"),
            _postcond_file_contains("test_app.py", "def test_"),
            _postcond_file_contains("test_app.py", "TestClient"),
            postcond_pytest_passes("test_app.py"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_skeleton", "write_requirements"),
        output_path="test_app.py",
    ))

    # --- 13. integration_check (terminal gate) ---
    tasks.append(_task(
        "integration_check",
        "tester",
        _step(
            step="Confirm the repo is complete. DO NOT modify any files.",
            inputs="The entire workspace.",
            output="(no new file — this task only verifies)",
            requirement="Just call mark_complete. Gate postconditions run "
                        "automatically: app.py imports cleanly, no module-scope "
                        "code after `if __name__`, requirements.txt parses, "
                        "test_app.py passes pytest.",
            tools="1) mark_complete summary='integration OK' artifacts=[].",
        ),
        postconditions=(
            postcond_python_imports("app.py"),
            postcond_no_code_after_main_block("app.py"),
            postcond_requirements_parse("requirements.txt"),
            postcond_pytest_passes("test_app.py"),
            _postcond_mark_complete(),
        ),
        depends_on=("build_delete", "write_requirements", "write_readme", "write_tests"),
    ))

    return tasks


def build_staged_tasks(tasks: list[Task]) -> dict[str, StagedTask]:
    """Stage the tasks where weak models consistently fail."""
    by_id = {t.id: t for t in tasks}
    staged: dict[str, StagedTask] = {}

    # write_requirements: mechanical single-line template.
    if "write_requirements" in by_id:
        staged["write_requirements"] = StagedTask(
            task=by_id["write_requirements"],
            stages=[
                Stage(
                    name="write",
                    instruction=(
                        "Write the file `requirements.txt` with EXACTLY these four lines "
                        "(no import statements, no comments):\n\n"
                        "fastapi>=0.110\n"
                        "uvicorn[standard]>=0.27\n"
                        "pydantic>=2.0\n"
                        "httpx>=0.25\n\n"
                        "Call write_file path='requirements.txt' "
                        "content='fastapi>=0.110\\nuvicorn[standard]>=0.27\\npydantic>=2.0\\nhttpx>=0.25\\n'."
                    ),
                    max_iterations=4,
                ),
            ],
        )

    # build_skeleton: verbatim template.
    if "build_skeleton" in by_id:
        staged["build_skeleton"] = StagedTask(
            task=by_id["build_skeleton"],
            stages=[
                Stage(
                    name="write_app_skeleton",
                    instruction=(
                        "Write `app.py` with this exact structure:\n\n"
                        "```python\n"
                        "import uvicorn\n"
                        "from fastapi import FastAPI\n"
                        "from pydantic import BaseModel\n"
                        "\n"
                        "\n"
                        "class Item(BaseModel):\n"
                        "    name: str\n"
                        "    price: float\n"
                        "\n"
                        "\n"
                        "app = FastAPI()\n"
                        "items: dict[int, Item] = {}\n"
                        "next_id: int = 1\n"
                        "\n"
                        "\n"
                        "if __name__ == '__main__':\n"
                        "    uvicorn.run(app)\n"
                        "```\n\n"
                        "Call write_file path='app.py' content=<the code above verbatim>."
                    ),
                    context_files=("design/modules.md", "design/endpoints.md"),
                    max_iterations=6,
                ),
            ],
        )

    # build_create: single edit_file_insert_before.
    if "build_create" in by_id:
        staged["build_create"] = StagedTask(
            task=by_id["build_create"],
            stages=[
                Stage(
                    name="add_create",
                    instruction=(
                        "Add the POST /items endpoint to app.py. Call ONE tool:\n\n"
                        "edit_file_insert_before(\n"
                        "    path='app.py',\n"
                        "    anchor=\"if __name__\",\n"
                        "    snippet=\"\"\"\n"
                        "@app.post('/items')\n"
                        "def create_item(item: Item):\n"
                        "    global next_id\n"
                        "    item_id = next_id\n"
                        "    items[item_id] = item\n"
                        "    next_id += 1\n"
                        "    return {'id': item_id, 'item': item}\n"
                        "\n"
                        "\"\"\",\n"
                        ")\n\n"
                        "Do NOT call write_file. Do NOT call edit_file_append — "
                        "handlers MUST appear BEFORE `if __name__`."
                    ),
                    context_files=("app.py",),
                    max_iterations=5,
                ),
            ],
        )

    # build_list: two edits — HTTPException import + GET handlers.
    if "build_list" in by_id:
        staged["build_list"] = StagedTask(
            task=by_id["build_list"],
            stages=[
                Stage(
                    name="add_list",
                    instruction=(
                        "Add GET /items and GET /items/{item_id}. Call TWO tools:\n\n"
                        "1) edit_file_replace(\n"
                        "     path='app.py',\n"
                        "     old_string='from fastapi import FastAPI',\n"
                        "     new_string='from fastapi import FastAPI, HTTPException',\n"
                        "   )\n\n"
                        "2) edit_file_insert_before(\n"
                        "     path='app.py',\n"
                        "     anchor=\"if __name__\",\n"
                        "     snippet=\"\"\"\n"
                        "@app.get('/items')\n"
                        "def list_items():\n"
                        "    return items\n"
                        "\n"
                        "@app.get('/items/{item_id}')\n"
                        "def read_item(item_id: int):\n"
                        "    if item_id not in items:\n"
                        "        raise HTTPException(status_code=404, detail='not found')\n"
                        "    return items[item_id]\n"
                        "\n"
                        "\"\"\",\n"
                        "   )\n\n"
                        "Do NOT call write_file. Do NOT call edit_file_append."
                    ),
                    context_files=("app.py",),
                    max_iterations=6,
                ),
            ],
        )

    # build_update: single insertion.
    if "build_update" in by_id:
        staged["build_update"] = StagedTask(
            task=by_id["build_update"],
            stages=[
                Stage(
                    name="add_update",
                    instruction=(
                        "Add PUT /items/{item_id}. Call ONE tool:\n\n"
                        "edit_file_insert_before(\n"
                        "    path='app.py',\n"
                        "    anchor=\"if __name__\",\n"
                        "    snippet=\"\"\"\n"
                        "@app.put('/items/{item_id}')\n"
                        "def update_item(item_id: int, item: Item):\n"
                        "    if item_id not in items:\n"
                        "        raise HTTPException(status_code=404, detail='not found')\n"
                        "    items[item_id] = item\n"
                        "    return {'id': item_id, 'item': item}\n"
                        "\n"
                        "\"\"\",\n"
                        ")\n\n"
                        "Do NOT call write_file. Do NOT call edit_file_append."
                    ),
                    context_files=("app.py",),
                    max_iterations=5,
                ),
            ],
        )

    # build_delete: single insertion.
    if "build_delete" in by_id:
        staged["build_delete"] = StagedTask(
            task=by_id["build_delete"],
            stages=[
                Stage(
                    name="add_delete",
                    instruction=(
                        "Add DELETE /items/{item_id}. Call ONE tool:\n\n"
                        "edit_file_insert_before(\n"
                        "    path='app.py',\n"
                        "    anchor=\"if __name__\",\n"
                        "    snippet=\"\"\"\n"
                        "@app.delete('/items/{item_id}')\n"
                        "def delete_item(item_id: int):\n"
                        "    if item_id not in items:\n"
                        "        raise HTTPException(status_code=404, detail='not found')\n"
                        "    del items[item_id]\n"
                        "    return {'deleted': item_id}\n"
                        "\n"
                        "\"\"\",\n"
                        ")\n\n"
                        "Do NOT call write_file. Do NOT call edit_file_append."
                    ),
                    context_files=("app.py",),
                    max_iterations=5,
                ),
            ],
        )

    # write_tests: verbatim pytest template.
    if "write_tests" in by_id:
        staged["write_tests"] = StagedTask(
            task=by_id["write_tests"],
            stages=[
                Stage(
                    name="write_tests",
                    instruction=(
                        "Write `test_app.py` with this exact template:\n\n"
                        "```python\n"
                        "from fastapi.testclient import TestClient\n"
                        "import app as app_module\n"
                        "\n"
                        "client = TestClient(app_module.app)\n"
                        "\n"
                        "\n"
                        "def test_create_and_list():\n"
                        "    app_module.items.clear()\n"
                        "    app_module.next_id = 1\n"
                        "    resp = client.post('/items', json={'name': 'x', 'price': 1.0})\n"
                        "    assert resp.status_code == 200\n"
                        "    resp = client.get('/items')\n"
                        "    assert resp.status_code == 200\n"
                        "    assert len(resp.json()) == 1\n"
                        "\n"
                        "\n"
                        "def test_404_on_missing():\n"
                        "    app_module.items.clear()\n"
                        "    resp = client.get('/items/999')\n"
                        "    assert resp.status_code == 404\n"
                        "```\n\n"
                        "Call write_file path='test_app.py' content=<the code above verbatim>."
                    ),
                    context_files=("app.py",),
                    max_iterations=6,
                ),
            ],
        )

    return staged


async def _preflight() -> None:
    print(f"[*] VRAM check for {LLM_MODEL}...")
    check = await check_model_fits(LLM_MODEL, base_url=OLLAMA_BASE_URL)
    print(f"  {check.reason}")
    raise_if_wont_fit(check, LLM_MODEL)


async def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    KB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    await _preflight()

    print(f"[*] Logging into Conduit as {SYSTEM_USER}")
    client = AgoraMatrixClient(homeserver=HOMESERVER, user_id=SYSTEM_USER)
    require_secret("AGORA_MATRIX_PASSWORD", SYSTEM_PASSWORD)
    await client.login(SYSTEM_PASSWORD)

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

    print("[*] Running project 'fastapi-crud' (observer enabled)")
    print(f"   open Element as {OBSERVER_USER} to watch")
    print(f"   review_timeout_seconds={REVIEW_TIMEOUT}")
    print()
    try:
        tasks = build_tasks()
        staged = build_staged_tasks(tasks)
        result = await orchestrator.run_project(
            "fastapi-crud",
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
