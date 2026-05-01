"""Inner tool definitions and executors per agent role.

Tools are described to the LLM as JSON-schema-ish dicts. At execution time the
runtime resolves a tool call's ``name`` to a coroutine via :func:`get_tool_executor`
and feeds back the result.

Tool categories:
- ``research``: ``web_search``, ``fetch_url`` (optional — require injected callables)
- ``filesystem``: ``read_file``, ``write_file``, ``list_directory`` (scoped to ``work_dir``)
- ``git``: ``git_commit``, ``git_diff``, ``git_log`` (Sprint 4 wires in a real RepoManager)
- ``coordination``: ``report_progress``, ``request_review``, ``mark_complete``, ``report_learning``
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agora.core.errors import AgoraError
from agora.core.types import AgentRole, RoomId
from agora.matrix.client import MatrixClientProtocol

logger = logging.getLogger(__name__)

ROLE_TOOL_SETS: dict[AgentRole, tuple[str, ...]] = {
    AgentRole.ARCHITECT: ("research", "filesystem", "coordination", "plan_authoring"),
    AgentRole.IMPLEMENTER: ("research", "filesystem", "git", "coordination"),
    AgentRole.REVIEWER: ("research", "filesystem", "git", "coordination"),
    AgentRole.TESTER: ("research", "filesystem", "git", "coordination"),
}


@dataclass
class ToolContext:
    """Per-task execution context threaded through every inner tool call.

    The ``ToolContext`` holds the work directory, the Matrix client, the
    git repo manager, and per-task mutable state (``written_files``,
    ``progress_log``, ``completions``, ``reported_learnings``, the
    ``plan_draft`` for plan-authoring stages). It's the single object every
    tool factory receives, so adding a new piece of per-task state is a
    one-line change here.

    Several flags gate what tools are exposed to the model:

    - ``auto_hooks_enabled`` — when True, the framework runs validation +
      ``git_commit`` + ``mark_complete`` automatically after ``write_file``,
      and those tools are hidden from the LLM manifest.
    - ``plan_authoring_enabled`` — gates the ``plan_*`` tool category;
      only the plan-builder runner enables this.
    - ``write_file_blocked`` — flipped by the overwrite guard once the
      model tries to clobber a file outside its declared output path. Stays
      set for the rest of the task so ``write_file`` is dropped from the
      manifest, forcing the model onto the edit primitives.

    Most fields default to safe no-op values; tests construct ``ToolContext``
    instances with fakes for ``matrix_client`` and ``git_repo`` to exercise
    tools in isolation.
    """

    work_dir: str
    matrix_client: MatrixClientProtocol
    agent_room_id: RoomId
    project_room_id: RoomId
    git_repo: Any = None  # RepoManager (Sprint 4), wired by orchestrator
    search_fn: Callable[[str], Awaitable[str]] | None = None
    fetch_fn: Callable[[str], Awaitable[str]] | None = None
    knowledge_refs: list[str] = field(default_factory=list)  # MXC URIs from identity
    knowledge_fetcher: Callable[[str], Awaitable[str]] | None = None  # mxc -> local path
    control: Any = None  # OrchestratorControl | None (forward ref to avoid cycle)
    progress_log: list[dict[str, Any]] = field(default_factory=list)
    reviews_requested: list[dict[str, Any]] = field(default_factory=list)
    completions: list[dict[str, Any]] = field(default_factory=list)
    reported_learnings: list[dict[str, Any]] = field(default_factory=list)
    # Files written via ``write_file`` this task. Populated by the tool
    # executor; used by auto-hooks to derive git-commit messages and
    # synthesized ``mark_complete`` artifact lists.
    written_files: list[str] = field(default_factory=list)
    # v2.4: once write_file's overwrite guard fires on any path this task has
    # not written itself, flip this flag — the per-turn tool filter then drops
    # write_file from the manifest entirely, forcing the model onto
    # edit_file_replace / edit_file_insert_before / edit_file_append for the
    # rest of the task. One guard failure is enough evidence that write_file
    # isn't the right tool here.
    write_file_blocked: bool = False
    # When True, the framework runs validation + git + mark_complete
    # automatically after write_file, and ``get_tool_definitions`` hides
    # those tools from the LLM. Set by the orchestrator for weak models.
    auto_hooks_enabled: bool = False
    # The task's declared primary output path (from Task.output_path). When
    # set, ``write_file`` logs a warning if the agent writes elsewhere.
    # Advisory only — the framework never rejects the write; the mismatch
    # just surfaces in logs and can be wired into postconditions later.
    expected_output_path: str = ""
    # Focus string (typically the current task description) that
    # :func:`agora.fleet.distiller.distill` uses to decide which parts of a
    # large read_file result to keep.
    task_focus: str = ""
    # Async callable ``(text, focus) -> str`` that shrinks large read_file
    # results to a safe size via hierarchical map-reduce summarization. When
    # None (the default), large reads pass through unchanged.
    distill_fn: Callable[[str, str], Awaitable[str]] | None = None
    # File size above which read_file auto-invokes ``distill_fn`` (in chars,
    # not bytes — close enough for ASCII-ish text).
    read_distill_threshold: int = 8_000
    # Mutable plan state for tool-driven plan authoring. Lazy-initialised on
    # the first ``plan_*`` tool call so non-planner tasks never allocate. The
    # attribute type is ``Any`` to avoid importing :mod:`agora.plan.builder`
    # at module-load time (keeps cycle-risk zero).
    plan_draft: Any = None
    # Gate for the ``plan_authoring`` tool category. Only the plan-builder
    # runner sets this True; every other runner (scripts/run_plan.py, the
    # discord-bot runner, etc.) leaves it False so emitted plans don't
    # accidentally expose ``plan_upsert_agent`` / ``plan_add_task_spec`` to
    # the implementing architect role.
    plan_authoring_enabled: bool = False
    # v2.7: path (work_dir-relative) of the scaffolded test file the current
    # LLM stage is filling. Set by the stage runner around ``fill_assertions``
    # stages and cleared afterwards. The ``fill_test_body`` tool reads this
    # to know WHICH file to edit without the model having to supply a path
    # — removes one source of 7B confusion. Empty → tool is hidden from the
    # LLM's manifest.
    active_test_file: str = ""


# ============================ Tool descriptions ============================

_RESEARCH_TOOLS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": "Search the web for a query. Returns summarized results.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch a URL and either return the text or save it to a file. "
            "PREFER save_as for any non-trivial page — echoing large fetched "
            "content back through a write_file call is unreliable. When "
            "save_as is set, the tool writes the fetched text to that path "
            "(relative to work_dir) and returns a short confirmation string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "save_as": {
                    "type": "string",
                    "description": (
                        "Relative path under work_dir. When set, fetched text "
                        "is written to this file and the tool returns a short "
                        "summary instead of the full content."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "recall_knowledge",
        "description": (
            "Search the agent's uploaded knowledge base (files attached to the "
            "identity room) by keyword. Returns up to 3 excerpts with filename "
            "and line range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

_FILESYSTEM_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file relative to the agent's work_dir.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write text content to a file relative to work_dir. Creates parents. "
            "REJECTS writes that would overwrite a file this task did not "
            "already create — use edit_file_replace / edit_file_insert_before / "
            "edit_file_append to modify pre-existing files. Pass force=true "
            "ONLY if you truly intend a full overwrite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file_replace",
        "description": (
            "Replace a unique substring in an existing file. `old_string` must "
            "appear EXACTLY ONCE; include enough surrounding context to make it "
            "unique. The file is not re-emitted — only the replacement happens. "
            "Prefer this over write_file for targeted substitutions (fixing a "
            "typo, tweaking one line, changing a function body)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "edit_file_insert_before",
        "description": (
            "Insert a snippet as new lines directly above the line containing "
            "`anchor` in an existing file. `anchor` is a substring that must "
            "appear in exactly one line. Use this for ADDING new code to an "
            "existing file (e.g. a new decorator block before `if __name__`). "
            "You only emit the snippet — the existing file is not re-emitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "anchor": {"type": "string"},
                "snippet": {"type": "string"},
            },
            "required": ["path", "anchor", "snippet"],
        },
    },
    {
        "name": "edit_file_append",
        "description": (
            "Append a snippet to the end of an existing file. The file is not "
            "re-emitted. Use this when adding a new function / command / test "
            "at the end of an existing file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "snippet": {"type": "string"},
            },
            "required": ["path", "snippet"],
        },
    },
    {
        "name": "add_function",
        "description": (
            "Append a new top-level function to a Python module, or replace "
            "an existing function with the same name. PREFERRED over "
            "edit_file_replace / edit_file_append when authoring or updating "
            "Python code — the framework handles positional placement and "
            "will never duplicate a definition.\n\n"
            "Supply `path` (the .py file) and `code` (complete python source "
            "for ONE function: the `def name(...):` line, body, and any "
            "decorators above it). Idempotent on name: repeated calls with "
            "the same `name` replace the existing function in place."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "code": {
                    "type": "string",
                    "description": (
                        "Complete Python source for ONE function. Example: "
                        "'def shorten(url):\\n    return url[:6]'. Must parse "
                        "as a single FunctionDef or AsyncFunctionDef."
                    ),
                },
            },
            "required": ["path", "code"],
        },
    },
    {
        "name": "add_class",
        "description": (
            "Append a new top-level class to a Python module, or replace an "
            "existing class with the same name. Use this to author CLASS "
            "SKELETONS — a class with its own __init__ and maybe a couple "
            "of method stubs. For adding further methods, prefer "
            "add_class_method so each method is a small isolated edit.\n\n"
            "Supply `path` and `code` (a complete `class Name(Bases):` "
            "block including its body). Idempotent on name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "code": {
                    "type": "string",
                    "description": (
                        "Complete Python source for ONE class (class line + "
                        "body). Must parse as a single ClassDef."
                    ),
                },
            },
            "required": ["path", "code"],
        },
    },
    {
        "name": "add_class_method",
        "description": (
            "Append a new method to a class, or replace an existing method "
            "with the same name inside that class. PREFERRED for adding "
            "behavior to an existing class — no whitespace matching, no "
            "duplicate definitions, framework handles indentation.\n\n"
            "Supply `path` (the .py file), `class_name` (the target class), "
            "and `code` (complete `def method(self, ...):` source — no "
            "`class` wrapper). Idempotent on method name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "class_name": {"type": "string"},
                "code": {
                    "type": "string",
                    "description": (
                        "Complete source for ONE method (including the `def` "
                        "line and body; no enclosing `class`). The framework "
                        "indents it to match the class body."
                    ),
                },
            },
            "required": ["path", "class_name", "code"],
        },
    },
    {
        "name": "fill_test_body",
        "description": (
            "Replace the body of a pytest test function with assertion code. "
            "PREFERRED for filling pytest.skip stubs during a fill_assertions "
            "stage — the framework handles file path, indentation, and "
            "docstring preservation. Only available during a fill_assertions "
            "stage (otherwise hidden from manifest). "
            "Supply `test_name` (e.g. 'test_add') and `body_code` (1+ lines "
            "of python: import + one or more `assert` statements). The "
            "framework replaces the entire function body atomically. "
            "Much more reliable than edit_file_replace for this task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_name": {
                    "type": "string",
                    "description": (
                        "Name of the test function, e.g. 'test_add'."
                    ),
                },
                "body_code": {
                    "type": "string",
                    "description": (
                        "Python body code. 1+ lines. The framework dedents "
                        "and re-indents to match the function — you don't "
                        "need to worry about leading whitespace."
                    ),
                },
            },
            "required": ["test_name", "body_code"],
        },
    },
    {
        "name": "list_directory",
        "description": "List entries in a directory relative to work_dir.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
            "required": [],
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Delete a file (or empty directory) relative to work_dir. Use this "
            "ONLY to resolve a genuine conflict — e.g. when a same-stem module "
            "file AND package directory exist simultaneously (src/foo.py + "
            "src/foo/) and you need to remove one to unblock imports. Does NOT "
            "recurse into non-empty directories; returns an error if the target "
            "has content. Returns 'deleted <path>' on success."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "check_python",
        "description": (
            "Compile-check AND undefined-name-check a Python file relative to "
            "work_dir. Catches hallucinated keywords, missing imports, and "
            "typos in module-scope references (e.g. using `os` without "
            "`import os`). Returns 'OK: <path>' on success, a SyntaxError "
            "message on parse failure, or a list of undefined module-scope "
            "names (with line numbers) when the file parses but references "
            "names that aren't imported/defined. Does NOT execute the module."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

_GIT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "git_commit",
        "description": "Stage all changes and commit with a message.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "git_diff",
        "description": "Return the current uncommitted diff.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "git_log",
        "description": "Return the last N commits (default 10).",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
]

_COORDINATION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "report_progress",
        "description": "Report interim progress to the project room.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "request_review",
        "description": "Ask a reviewer agent or human to review the current work.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    {
        "name": "mark_complete",
        "description": "Mark the current task complete with a final summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "report_learning",
        "description": "Record a reusable lesson for future runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["pattern", "failure", "preference", "tool_usage"],
                },
                "content": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["category", "content"],
        },
    },
    {
        "name": "post_note",
        "description": (
            "Post a plain ``m.room.message`` to the project room so the user sees "
            "it in Element. Use this to share intermediate artifacts (e.g. a draft "
            "brief or proposed task list) before asking for approval via "
            "``await_user_decision``. The body is rendered as markdown in "
            "clients that support it. Returns 'note posted'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
        },
    },
    {
        "name": "await_user_decision",
        "description": (
            "Ask the human user a blocking design question via a Matrix poll "
            "and BLOCK until they click an option. Use this during plan "
            "authoring to surface decisions the user must make (e.g. 'Should "
            "storage be JSON or SQLite?'). Returns the chosen option's id as "
            "the tool result; you then use that id in subsequent tool calls. "
            "``decision_id`` must be a unique stable id (e.g. 'storage') so "
            "the framework can route the response; ``options`` is a list of "
            "answer ids to present. Times out after 5 minutes by default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "decision_id": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                },
                "timeout_seconds": {"type": "number", "default": 300},
            },
            "required": ["question", "decision_id", "options"],
        },
    },
]

_RUNTIME_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_python_import",
        "description": (
            "Spawn a Python subprocess and import the given module file "
            "relative to work_dir. Returns 'OK: imported <path>' on success, "
            "or the traceback (exit code, stderr, stdout) on failure. Catches "
            "AttributeError, ImportError, NameError, and other eager-import "
            "failures that static checks miss. Times out after 15s. Env sets "
            "DISCORD_TOKEN=dummy so module-scope env lookups do not KeyError."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_pytest",
        "description": (
            "Run pytest against a file or directory under work_dir. Returns "
            "'OK: <summary>' when the suite exits 0, or 'PYTEST FAILED: ...' "
            "with the short summary when it fails. Use this to verify tests "
            "actually pass before marking work complete. Times out after 60s."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "maxfail": {"type": "integer", "default": 1},
            },
            "required": [],
        },
    },
    {
        "name": "check_requirements",
        "description": (
            "Validate each non-blank non-comment line of requirements.txt "
            "parses as a PEP 508 requirement. Returns 'OK: <N> requirements' "
            "or a line-by-line list of parse errors. Catches stray statements, "
            "typos, and malformed specs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "requirements.txt"}},
            "required": [],
        },
    },
]

_PLAN_AUTHORING_TOOLS: list[dict[str, Any]] = [
    {
        "name": "plan_upsert_agent",
        "description": (
            "Add or update ONE agent in the plan you are authoring (idempotent "
            "by name — calling twice with the same name updates the entry). "
            "Each per-agent author stage calls this exactly once. ``role`` "
            "must be one of 'architect', 'implementer', 'reviewer', 'tester'. "
            "``instructions`` should be 2-4 sentences describing what this "
            "agent is responsible for. Returns a short confirmation string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "instructions": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["name", "role", "instructions"],
        },
    },
    {
        "name": "plan_set_agents",
        "description": (
            "DEPRECATED — use plan_upsert_agent once per agent. Declares the "
            "full agent roster in one call. Pass a list of "
            "{name, role, instructions, model?} dicts. Kept for back-compat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "instructions": {"type": "string"},
                            "model": {"type": "string"},
                        },
                        "required": ["name", "role"],
                    },
                }
            },
            "required": ["agents"],
        },
    },
    {
        "name": "plan_add_task",
        "description": (
            "Add one task to the plan you are authoring. Task id must be a "
            "snake_case identifier, unique within the plan. ``assigned_to`` "
            "must match an agent name registered via plan_set_agents. "
            "``depends_on`` is a list of already-added task ids (order "
            "matters — add dependencies before their dependents). "
            "``output_path`` is the task's primary output file (relative to "
            "work_dir). Returns a confirmation + current task count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "description": {"type": "string"},
                "assigned_to": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "output_path": {"type": "string"},
            },
            "required": ["task_id", "description", "assigned_to"],
        },
    },
    {
        "name": "plan_add_task_spec",
        "description": (
            "Atomic: add ONE task to the plan together with its postconditions "
            "in a single call. Use this as the standard way to author tasks — "
            "each per-task author stage makes exactly ONE plan_add_task_spec "
            "call. Pass ``postconditions`` as a list of {name, args?} dicts. "
            "The framework auto-fills the ``rel`` arg from ``output_path`` for "
            "postconditions that need it (file_exists, py_compiles, "
            "file_contains, no_code_after_main_block, max_line_length, "
            "python_imports) — you can pass a minimal shape like "
            "[{\"name\": \"file_exists\"}, {\"name\": \"py_compiles\"}, "
            "{\"name\": \"mark_complete\"}] and the framework fills in rel. "
            "If a postcondition needs an arg the framework can't infer (e.g. "
            "file_contains.substring), pass it explicitly via ``args``. "
            "Minimum acceptable postconditions = [{\"name\": \"mark_complete\"}]. "
            "If any postcondition is invalid, the whole task is rolled back "
            "and you can retry. ``depends_on`` must reference task ids "
            "already in the draft."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "description": {"type": "string"},
                "assigned_to": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "output_path": {"type": "string"},
                "postconditions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": [
                "task_id", "description", "assigned_to", "postconditions",
            ],
        },
    },
    {
        "name": "plan_attach_postcondition",
        "description": (
            "Attach a postcondition from the framework's registry to a task "
            "you have already added. ``name`` must be a registered predicate "
            "name (see plan/kb/postcondition_catalog.md). ``args`` is the "
            "keyword arguments for that predicate factory. Invalid names or "
            "arg shapes are rejected immediately — check the catalog. Call "
            "this one or more times per task; every task must end up with at "
            "least one postcondition or plan_finalize will refuse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "name": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["task_id", "name"],
        },
    },
    {
        "name": "plan_add_llm_stage",
        "description": (
            "Attach a staged LLM-driven sub-step to a task. ``instruction`` "
            "is the text the LLM will see when the stage runs. "
            "``context_files`` are workspace-relative paths whose contents "
            "are pre-loaded into the stage's user message. ``max_iterations`` "
            "caps how many tool-call turns the stage gets. Stage names must "
            "be unique within a task. Optional — tasks without stages run "
            "the task's description directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "name": {"type": "string"},
                "instruction": {"type": "string"},
                "context_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "max_iterations": {"type": "integer"},
            },
            "required": ["task_id", "name", "instruction"],
        },
    },
    {
        "name": "plan_add_decision_stage",
        "description": (
            "Attach a declarative decision stage to a task. The framework "
            "will post the ``question`` + ``options`` to the project room as "
            "a Matrix poll + question card + /agora decision chat fallback "
            "when the plan runs, await the user's answer, and write the "
            "chosen answer id to ``output_path``. ``decision_id`` must be "
            "unique across the entire plan. ``options`` must have ≥ 2 entries "
            "and will each get a numbered-emoji reaction (1️⃣, 2️⃣, …). No "
            "LLM is consulted at decision time — the framework handles it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "name": {"type": "string"},
                "decision_id": {"type": "string"},
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                },
                "output_path": {"type": "string"},
            },
            "required": [
                "task_id", "name", "decision_id", "question", "options", "output_path",
            ],
        },
    },
    {
        "name": "plan_finalize",
        "description": (
            "Serialize the plan you have built to a YAML file and validate "
            "it round-trips cleanly through the framework's loader. The "
            "emitted file is ready to execute via ``scripts/run_plan.py``. "
            "Returns OK + agent/task/stage counts on success, or an ERROR "
            "string listing missing pieces so you can retry. Call this as "
            "the last tool call in your plan-authoring sequence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "default": "plan/out.plan.yaml",
                },
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": [],
        },
    },
]


_CATEGORY_DEFS: dict[str, list[dict[str, Any]]] = {
    "research": _RESEARCH_TOOLS,
    "filesystem": _FILESYSTEM_TOOLS,
    "git": _GIT_TOOLS,
    "runtime": _RUNTIME_TOOLS,
    "coordination": _COORDINATION_TOOLS,
    "plan_authoring": _PLAN_AUTHORING_TOOLS,
}


#: Tool names hidden from the LLM when ``ToolContext.auto_hooks_enabled`` is True.
#: The framework runs these automatically after ``write_file``, so exposing them
#: to weak models only adds cognitive overhead.
AUTO_HOOKED_TOOL_NAMES: frozenset[str] = frozenset(
    # v2.7: mark_complete is INTENTIONALLY NOT in this set even though
    # auto-hooks synthesize it when the model never calls it. Hiding
    # mark_complete from weak models causes them to reach for the closest
    # similar tool (post_note) and invent wrong argument shapes, burning
    # entire retry budgets on "ERROR: body is required". Synthesis only
    # fires when ctx.completions is empty, so visible mark_complete + the
    # safety-net synthesis don't collide.
    {"check_python", "git_commit", "git_diff", "git_log", "report_learning"}
)

#: Plan-authoring tools that are always hidden from the LLM manifest but still
#: callable via :func:`get_tool_executor` (so tests and back-compat consumers
#: can drive them directly). The compound :tool:`plan_add_task_spec` is the
#: only task-authoring surface the model sees — giving weak 7B planners the
#: choice between ``plan_add_task_spec`` and the bare ``plan_add_task`` caused
#: half-authored tasks without postconditions (see live runs where the model
#: mixed both tools within a single author_tasks stage).
_LLM_HIDDEN_PLAN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "plan_add_task",
        "plan_attach_postcondition",
        "plan_add_llm_stage",
        "plan_add_decision_stage",
        # v2.3: hide the blob-set tool now that plan_upsert_agent exists — gave
        # the 7B a way to overwrite the entire roster mid-stream and trash
        # already-authored agents' identities.
        "plan_set_agents",
    }
)


def get_tool_definitions(
    role: AgentRole,
    *,
    auto_hooks_enabled: bool = False,
    plan_authoring_enabled: bool = False,
    fill_test_body_bound: bool = False,
) -> list[dict[str, Any]]:
    """Return the tool schemas for the given role.

    Always hides the deprecated granular plan-authoring tools. When
    ``auto_hooks_enabled`` is set, additionally hides tools whose behaviour
    is already automated by :mod:`agora.fleet.auto_hooks`.

    When ``plan_authoring_enabled`` is False (default), drops the entire
    ``plan_authoring`` category — the plan-builder runner opts in via
    ToolContext; other runners don't, so emitted plans whose architect
    role would otherwise see plan_upsert_agent / plan_add_task_spec don't
    accidentally drift into meta-authoring calls. Also hides
    ``await_user_decision`` in the build path: the plan-builder legitimately
    drives user decisions via declarative decision STAGES (framework-owned);
    build agents calling ``await_user_decision`` ad-hoc blocks on 300s
    timeouts without advancing the task — observed disruption in live
    url-shortener build runs.

    v2.7: ``fill_test_body_bound`` gates the ``fill_test_body`` tool. It
    only makes sense inside a ``fill_assertions`` stage where the stage
    runner has bound ``ctx.active_test_file``; everywhere else the tool
    is hidden so the model can't pick it up as a generic file-editor.
    """
    tools: list[dict[str, Any]] = []
    for category in ROLE_TOOL_SETS.get(role, ()):
        if category == "plan_authoring" and not plan_authoring_enabled:
            continue
        tools.extend(_CATEGORY_DEFS[category])
    tools = [t for t in tools if t["name"] not in _LLM_HIDDEN_PLAN_TOOL_NAMES]
    # v2.7: await_user_decision always hidden from LLM manifests. The
    # framework owns user decisions via ``kind=decision`` stages (declarative,
    # no LLM call); when an LLM stage accidentally invokes await_user_decision
    # it blocks on a 300s timeout with no voter for a tool-level call,
    # stalling the whole phase. Observed in plan-builder gather_context
    # turn 3 — architect called the tool and hung for 4.5 min.
    tools = [t for t in tools if t["name"] != "await_user_decision"]
    if auto_hooks_enabled:
        tools = [t for t in tools if t["name"] not in AUTO_HOOKED_TOOL_NAMES]
    if not fill_test_body_bound:
        tools = [t for t in tools if t["name"] != "fill_test_body"]
    return tools


# =========================== Tool executor factories ===========================


def get_tool_executor(
    role: AgentRole, context: ToolContext
) -> dict[str, Callable[[dict[str, Any]], Awaitable[str]]]:
    """Return a ``name -> async callable`` mapping for the role's tools.

    Each callable accepts the LLM-provided arguments dict and returns a string
    result that is fed back to the model.
    """
    executor: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {}
    categories = ROLE_TOOL_SETS.get(role, ())

    if "research" in categories:
        executor["web_search"] = _make_search(context)
        executor["fetch_url"] = _make_fetch(context)
        executor["recall_knowledge"] = _make_recall_knowledge(context)
    if "filesystem" in categories:
        executor["read_file"] = _make_read(context)
        executor["write_file"] = _make_write(context, role)
        executor["list_directory"] = _make_list(context)
        executor["delete_file"] = _make_delete_file(context, role)
        executor["check_python"] = _make_check_python(context)
        executor["edit_file_replace"] = _make_edit_replace(context, role)
        executor["edit_file_insert_before"] = _make_edit_insert_before(context, role)
        executor["edit_file_append"] = _make_edit_append(context, role)
        executor["fill_test_body"] = _make_fill_test_body(context, role)
        # v2.7 Sprint 7.4: AST-aware upsert tools for python source files.
        executor["add_function"] = _make_add_function(context, role)
        executor["add_class"] = _make_add_class(context, role)
        executor["add_class_method"] = _make_add_class_method(context, role)
    if "git" in categories:
        executor["git_commit"] = _make_git_commit(context)
        executor["git_diff"] = _make_git_diff(context)
        executor["git_log"] = _make_git_log(context)
    if "runtime" in categories:
        executor["run_python_import"] = _make_run_python_import(context)
        executor["run_pytest"] = _make_run_pytest(context)
        executor["check_requirements"] = _make_check_requirements(context)
    if "coordination" in categories:
        executor["report_progress"] = _make_report_progress(context)
        executor["request_review"] = _make_request_review(context)
        executor["mark_complete"] = _make_mark_complete(context)
        executor["report_learning"] = _make_report_learning(context)
        executor["await_user_decision"] = _make_await_user_decision(context)
        executor["post_note"] = _make_post_note(context)
    if "plan_authoring" in categories:
        executor["plan_set_agents"] = _make_plan_set_agents(context)
        executor["plan_upsert_agent"] = _make_plan_upsert_agent(context)
        executor["plan_add_task"] = _make_plan_add_task(context)
        executor["plan_add_task_spec"] = _make_plan_add_task_spec(context)
        executor["plan_attach_postcondition"] = _make_plan_attach_postcondition(context)
        executor["plan_add_llm_stage"] = _make_plan_add_llm_stage(context)
        executor["plan_add_decision_stage"] = _make_plan_add_decision_stage(context)
        executor["plan_finalize"] = _make_plan_finalize(context)

    return executor


# ---- filesystem ----


def _safe_path(work_dir: str, rel: str) -> Path:
    base = Path(work_dir).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise AgoraError(f"path {rel!r} escapes work_dir") from exc
    return target


def _stem_collision(path: Path) -> Path | None:
    """Return the offending sibling path if writing ``path`` would create
    a module-vs-package stem collision, else None.

    Two cases:
      1. Writing ``src/foo.py`` when ``src/foo/`` (a directory) already
         exists — Python's package takes precedence over the module.
      2. Writing ``src/foo/__init__.py`` when ``src/foo.py`` already
         exists — same import trap, opposite direction.

    Both produce runtime ImportErrors that the 7B can't easily diagnose;
    catching them at write-time surfaces the conflict clearly.
    """
    if path.suffix == ".py" and path.stem != "__init__":
        # Case 1: src/foo.py + src/foo/ → collision
        sibling_dir = path.with_suffix("")
        if sibling_dir.is_dir():
            return sibling_dir
    elif path.name == "__init__.py":
        # Case 2: src/foo/__init__.py + src/foo.py → collision
        parent = path.parent
        sibling_module = parent.with_suffix(".py")
        if sibling_module.is_file():
            return sibling_module
    return None


def _make_read(ctx: ToolContext):
    async def read_file(args: dict[str, Any]) -> str:
        path = _safe_path(ctx.work_dir, args["path"])
        if not path.is_file():
            return f"ERROR: file not found: {args['path']}"
        body = path.read_text(encoding="utf-8")
        if (
            ctx.distill_fn is not None
            and len(body) > ctx.read_distill_threshold
            and ctx.task_focus
        ):
            try:
                return await ctx.distill_fn(body, ctx.task_focus)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "read_file distill failed for %s (%d chars): %s — returning "
                    "head-truncated body instead",
                    args["path"], len(body), exc,
                )
                return _head_truncate_plain(body, ctx.read_distill_threshold)
        return body

    return read_file


def _head_truncate_plain(text: str, limit: int) -> str:
    """Emergency fallback when the distiller itself fails. Head-truncation only."""
    if len(text) <= limit:
        return text
    marker = "\n\n[...truncated: file too large and distiller unavailable...]"
    return text[: max(0, limit - len(marker))] + marker


def _enforce_path_scope(role: AgentRole, rel: str, tool_label: str) -> None:
    """Raise :class:`AgoraError` if ``role`` is not allowed to write ``rel``.

    v2.7 scope enforcement. Observed failure: an implementer task whose
    ``pytest_passes(tests/test_contract.py)`` postcondition was failing
    "fixed" the problem by editing the contract test file itself. That
    corrupted the framework-authored contract and cascaded failures. The
    right response is "rewrite your implementation", not "edit the test".

    Scope rules:
      - ``tester`` owns ``tests/**`` and ``plan/kb/**`` (intent files).
      - ``implementer`` owns ``src/**``, ``requirements.txt``,
        ``pyproject.toml``, and top-level project files.
      - ``architect`` is intentionally broad (plan-builder authors plan/**)
        and not gated here.
      - ``reviewer`` is read-only; treat like architect for now (nothing to
        gate).

    Non-owners attempting to write to another role's turf get a clear error
    that tells them what to do instead.
    """
    # Architect / reviewer / unknown role: no restriction.
    if role not in (AgentRole.IMPLEMENTER, AgentRole.TESTER):
        return
    rel_norm = rel.replace("\\", "/").lstrip("/")
    if role is AgentRole.IMPLEMENTER and rel_norm.startswith("tests/"):
        raise AgoraError(
            f"{tool_label}: implementer role may not write to {rel!r}. "
            "tests/ is owned by the tester. The contract tests are "
            "authoritative — if a test fails, fix your implementation "
            "under src/, not the test."
        )
    if role is AgentRole.TESTER and rel_norm.startswith("src/"):
        raise AgoraError(
            f"{tool_label}: tester role may not write to {rel!r}. "
            "src/ is owned by the implementer. Adjust your assertions to "
            "match the real implementation shape; do not rewrite src/."
        )


#: Marker left in every seeded stub file (see
#: :func:`agora.plan.harness.seed_workspace`). When a Python source file
#: under src/ still contains this token, it's a Sprint 7.5 stub — the model
#: must use ``add_function`` / ``add_class`` / ``add_class_method`` (which
#: upsert by name) instead of the whitespace-matching edit tools, which
#: historically corrupt stubs with nested defs and orphan return statements.
_STUB_MARKER = "raise NotImplementedError"


def _reject_edit_on_stub(
    ctx: ToolContext, rel: str, tool_label: str
) -> None:
    """Raise :class:`AgoraError` if ``rel`` is a Sprint 7.5 stub file.

    Stub files are framework-authored and must be mutated via the AST-aware
    upsert tools (``add_function`` / ``add_class_method``). Letting the
    model use ``edit_file_replace`` / ``edit_file_append`` on them reliably
    produces corrupt Python: nested defs, orphan return statements,
    doubled definitions. Observed in Sprint 7.5d's ``lookup_hash`` failure.

    Check is per-path — once the model has REPLACED every
    ``raise NotImplementedError`` via upsert, the file is no longer a stub
    and edit tools become available again on subsequent iterations.
    """
    if not rel.endswith(".py") or "src/" not in rel.replace("\\", "/"):
        return
    path = _safe_path(ctx.work_dir, rel)
    if not path.is_file():
        return
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return
    if _STUB_MARKER not in body:
        return
    raise AgoraError(
        f"{tool_label}: {rel!r} is a Sprint 7.5 stub (contains "
        f"'raise NotImplementedError'). The whitespace-matching edit tools "
        f"corrupt stubs. Use add_class_method(path={rel!r}, class_name=..., "
        f"code=...) for methods or add_function(path={rel!r}, code=...) for "
        f"top-level functions — both upsert by name and preserve structure."
    )


def _record_write(ctx: ToolContext, rel: str, tool_label: str) -> None:
    """Shared bookkeeping for write-style tools: populate written_files + warn."""
    if rel not in ctx.written_files:
        ctx.written_files.append(rel)
    if ctx.expected_output_path and rel != ctx.expected_output_path:
        logger.warning(
            "%s path mismatch: wrote to %r but task expected %r",
            tool_label, rel, ctx.expected_output_path,
        )


def _format_match_locations(
    body: str,
    needle: str,
    *,
    max_matches: int = 5,
    context_lines: int = 1,
) -> str:
    """Render match locations of ``needle`` in ``body`` with line-numbered context.

    Returned string is suitable for embedding in a multi-match error so the
    model can see WHERE the duplicates are and pick disambiguating context
    in one turn instead of guessing blindly across multiple retries.

    Formatting:
      - One ``#N at line L:`` header per match
      - ``context_lines`` before and after each match, with line-number gutter
      - Match line gets a ``→`` marker in the gutter
      - Collapsed ``(+K more)`` footer when total matches exceed max_matches

    Uses character-offset positions (not line-by-line) because
    ``body.count(needle)`` is substring-aware — a match may span lines or
    be a fragment of a longer identifier (e.g. ``shortener`` inside
    ``url_shortener``). Line numbers are computed from the offset.
    """
    if not needle:
        return ""
    lines = body.splitlines()
    # Precompute line-start offsets so we can convert a char offset → (line_idx, col).
    line_starts: list[int] = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line) + 1)  # +1 for the newline

    def _offset_to_line(off: int) -> int:
        """Return 0-based line index containing offset ``off``."""
        # Binary search would be faster but file sizes here are tiny.
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if line_starts[mid] <= off:
                lo = mid + 1
            else:
                hi = mid
        # lo now points at the first line_start strictly > off; the match
        # begins on lo - 1 (clamped to 0).
        return max(0, lo - 1)

    positions: list[int] = []
    start = 0
    while True:
        idx = body.find(needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + max(1, len(needle))  # advance past this match

    if not positions:
        return ""

    blocks: list[str] = []
    show = positions[:max_matches]
    for i, pos in enumerate(show, 1):
        line_idx = _offset_to_line(pos)
        lo = max(0, line_idx - context_lines)
        hi = min(len(lines), line_idx + context_lines + 1)
        header = f"#{i} at line {line_idx + 1}:"
        rendered: list[str] = [header]
        for ln_idx in range(lo, hi):
            marker = "→" if ln_idx == line_idx else " "
            rendered.append(f"  {marker} L{ln_idx + 1:>3}: {lines[ln_idx]}")
        blocks.append("\n".join(rendered))
    if len(positions) > max_matches:
        blocks.append(f"  (+{len(positions) - max_matches} more match(es) not shown)")
    return "\n".join(blocks)


def _make_edit_replace(ctx: ToolContext, role: AgentRole = AgentRole.ARCHITECT):
    async def edit_file_replace(args: dict[str, Any]) -> str:
        rel = args["path"]
        _enforce_path_scope(role, rel, "edit_file_replace")
        _reject_edit_on_stub(ctx, rel, "edit_file_replace")
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            return f"ERROR: file not found: {rel}"
        old_string = args["old_string"]
        new_string = args["new_string"]
        if not old_string:
            raise AgoraError("edit_file_replace: old_string must not be empty")
        body = path.read_text(encoding="utf-8")
        count = body.count(old_string)
        if count == 0:
            raise AgoraError(
                f"edit_file_replace: old_string not found in {rel}. "
                f"Check exact whitespace/casing, or read_file first to see current content."
            )
        if count > 1:
            locations = _format_match_locations(body, old_string)
            raise AgoraError(
                f"edit_file_replace: old_string matches {count} places in {rel}; "
                f"must be unique.\n\n"
                f"Match locations:\n{locations}\n\n"
                f"To disambiguate, either:\n"
                f"  (a) add lines from the surrounding context shown above "
                f"(before or after) to your old_string so it uniquely identifies "
                f"ONE match;\n"
                f"  (b) if you're editing a method body, call "
                f"add_class_method(path={rel!r}, class_name=..., code=<full def>) "
                f"— it upserts by name and doesn't care about whitespace;\n"
                f"  (c) if you're editing a top-level function, call "
                f"add_function(path={rel!r}, code=<full def>) similarly."
            )
        new_body = body.replace(old_string, new_string, 1)
        path.write_text(new_body, encoding="utf-8")
        _record_write(ctx, rel, "edit_file_replace")
        delta = len(new_string) - len(old_string)
        return f"replaced {len(old_string)} chars in {rel} (delta {delta:+d})"

    return edit_file_replace


def _make_add_function(ctx: ToolContext, role: AgentRole = AgentRole.IMPLEMENTER):
    async def add_function(args: dict[str, Any]) -> str:
        rel = args.get("path", "")
        code = args.get("code", "")
        if not rel or not isinstance(rel, str):
            raise AgoraError("add_function: path must be a non-empty string")
        if not code or not isinstance(code, str):
            raise AgoraError("add_function: code must be a non-empty string")
        _enforce_path_scope(role, rel, "add_function")
        if not rel.endswith(".py"):
            raise AgoraError(
                f"add_function: path must be a .py file, got {rel!r}"
            )
        # v2.7 Sprint 7.6(e): detect methods-shaped code and redirect. 7B
        # repeatedly calls add_function with `def foo(self, ...)` at turn 2-4
        # of an implementer task, which lands top-level functions next to
        # the real class methods and clutters the file. If the code's first
        # arg is `self`, tell the model to call add_class_method — with the
        # enclosing class name picked automatically when the file has
        # exactly one class.
        import ast as _ast
        import textwrap as _tw

        try:
            probe = _ast.parse(_tw.dedent(code).strip("\n"))
        except SyntaxError:
            probe = None
        if probe and probe.body:
            fn0 = probe.body[0]
            if isinstance(fn0, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if fn0.args.args and fn0.args.args[0].arg == "self":
                    # Auto-route when file has exactly one class; else hint.
                    existing = _safe_path(ctx.work_dir, rel)
                    if existing.is_file():
                        try:
                            host_tree = _ast.parse(existing.read_text(encoding="utf-8"))
                            host_classes = [
                                n.name for n in host_tree.body
                                if isinstance(n, _ast.ClassDef)
                            ]
                        except SyntaxError:
                            host_classes = []
                    else:
                        host_classes = []
                    if len(host_classes) == 1:
                        # Delegate to add_class_method automatically.
                        # Sprint 7.7: apply method-name spec check on the
                        # auto-routed path too; otherwise this was a
                        # backdoor past the 7.7 validation.
                        _reject_class_not_in_spec(
                            ctx, host_classes[0], "add_function(autoroute)"
                        )
                        _reject_method_not_in_spec(
                            ctx,
                            host_classes[0],
                            fn0.name,
                            "add_function(autoroute)",
                        )
                        from agora.plan.module_editor import upsert_class_method

                        source = existing.read_text(encoding="utf-8")
                        try:
                            new_source = upsert_class_method(
                                source, host_classes[0], code
                            )
                        except (ValueError, SyntaxError) as exc:
                            raise AgoraError(
                                f"add_function: auto-routed to add_class_method "
                                f"for class {host_classes[0]!r} but upsert "
                                f"failed: {exc}"
                            ) from exc
                        existing.write_text(new_source, encoding="utf-8")
                        _record_write(ctx, rel, "add_function(autoroute)")
                        return (
                            f"auto-routed: fn {fn0.name!r} has `self` so "
                            f"upserted as method of {host_classes[0]!r} in {rel}"
                        )
                    raise AgoraError(
                        f"add_function: '{fn0.name}' has `self` as its first "
                        f"parameter — it's a METHOD, not a top-level function. "
                        f"Call add_class_method(path={rel!r}, class_name=..., "
                        f"code=...). Classes present in {rel}: "
                        f"{sorted(host_classes) if host_classes else '<none>'}."
                    )
        # Sprint 7.7(i): reject top-level functions not in api_spec.
        # (Self-first functions have already auto-routed above to
        # add_class_method, so anything reaching here is a genuine
        # top-level function.)
        if probe and probe.body and isinstance(
            probe.body[0], (_ast.FunctionDef, _ast.AsyncFunctionDef)
        ):
            _reject_function_not_in_spec(
                ctx, probe.body[0].name, "add_function"
            )
        path = _safe_path(ctx.work_dir, rel)
        source = path.read_text(encoding="utf-8") if path.is_file() else ""
        from agora.plan.module_editor import upsert_function

        try:
            new_source = upsert_function(source, code)
        except ValueError as exc:
            raise AgoraError(f"add_function: {exc}") from exc
        except SyntaxError as exc:
            raise AgoraError(
                f"add_function: your code or the resulting file is invalid "
                f"python. Details: {exc}"
            ) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_source, encoding="utf-8")
        _record_write(ctx, rel, "add_function")
        # Fish the function name out for a useful return message.
        import ast as _ast
        tree = _ast.parse(code)
        name = next(
            (n.name for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))),
            "?",
        )
        return f"upserted function {name!r} in {rel}"

    return add_function


def _make_add_class(ctx: ToolContext, role: AgentRole = AgentRole.IMPLEMENTER):
    async def add_class(args: dict[str, Any]) -> str:
        rel = args.get("path", "")
        code = args.get("code", "")
        if not rel or not isinstance(rel, str):
            raise AgoraError("add_class: path must be a non-empty string")
        if not code or not isinstance(code, str):
            raise AgoraError("add_class: code must be a non-empty string")
        _enforce_path_scope(role, rel, "add_class")
        if not rel.endswith(".py"):
            raise AgoraError(f"add_class: path must be a .py file, got {rel!r}")
        # Sprint 7.7(h): extract class name, reject if not in api_spec.
        # We parse the code first to get the name, then validate BEFORE
        # touching the filesystem.
        import ast as _ast
        import textwrap as _tw

        try:
            probe_tree = _ast.parse(_tw.dedent(code).strip("\n"))
        except SyntaxError:
            probe_tree = None
        probe_name: str | None = None
        if probe_tree and probe_tree.body:
            first = probe_tree.body[0]
            if isinstance(first, _ast.ClassDef):
                probe_name = first.name
        if probe_name:
            _reject_class_not_in_spec(ctx, probe_name, "add_class")
        path = _safe_path(ctx.work_dir, rel)
        source = path.read_text(encoding="utf-8") if path.is_file() else ""
        # Sprint 7.7 follow-up: prevent retry-wipe. If the class already
        # exists in the file with real method bodies, do NOT let add_class
        # replace it wholesale — that nukes progress when a retry fires
        # due to an unrelated postcondition failure. Force add_class_method
        # upserts instead.
        if probe_name and source:
            try:
                existing_tree = _ast.parse(source)
                for node in existing_tree.body:
                    if (
                        isinstance(node, _ast.ClassDef)
                        and node.name == probe_name
                    ):
                        filled_methods = [
                            n.name for n in node.body
                            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                            and not _method_body_is_stub(n)
                        ]
                        if filled_methods:
                            raise AgoraError(
                                f"add_class: {probe_name!r} already exists in "
                                f"{rel!r} with filled methods "
                                f"{sorted(filled_methods)}. Refusing to "
                                f"overwrite the whole class — use "
                                f"add_class_method(class_name={probe_name!r}, "
                                f"code=...) to add/replace individual methods."
                            )
                        break
            except SyntaxError:
                pass
        from agora.plan.module_editor import upsert_class

        try:
            new_source = upsert_class(source, code)
        except ValueError as exc:
            raise AgoraError(f"add_class: {exc}") from exc
        except SyntaxError as exc:
            raise AgoraError(
                f"add_class: invalid python. Details: {exc}"
            ) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_source, encoding="utf-8")
        _record_write(ctx, rel, "add_class")
        import ast as _ast
        tree = _ast.parse(code)
        name = next(
            (n.name for n in tree.body if isinstance(n, _ast.ClassDef)),
            "?",
        )
        return f"upserted class {name!r} in {rel}"

    return add_class


def _make_add_class_method(
    ctx: ToolContext, role: AgentRole = AgentRole.IMPLEMENTER
):
    async def add_class_method(args: dict[str, Any]) -> str:
        rel = args.get("path", "")
        class_name = args.get("class_name", "")
        code = args.get("code", "")
        if not rel or not isinstance(rel, str):
            raise AgoraError("add_class_method: path must be a non-empty string")
        if not class_name or not isinstance(class_name, str):
            raise AgoraError("add_class_method: class_name must be a non-empty string")
        if not code or not isinstance(code, str):
            raise AgoraError("add_class_method: code must be a non-empty string")
        _enforce_path_scope(role, rel, "add_class_method")
        if not rel.endswith(".py"):
            raise AgoraError(f"add_class_method: path must be a .py file, got {rel!r}")
        # Sprint 7.7(h): reject unknown class_name — if the spec declares
        # which classes exist, the implementer can only add methods to
        # those. Prevents reintroducing off-spec classes by the back door.
        _reject_class_not_in_spec(ctx, class_name, "add_class_method")
        # Sprint 7.7 follow-up: also reject unknown method_name. Parse the
        # code to extract the method name.
        import ast as _ast
        import textwrap as _tw

        try:
            probe_tree = _ast.parse(_tw.dedent(code).strip("\n"))
        except SyntaxError:
            probe_tree = None
        if probe_tree and probe_tree.body:
            probe_fn = probe_tree.body[0]
            if isinstance(probe_fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                _reject_method_not_in_spec(
                    ctx, class_name, probe_fn.name, "add_class_method"
                )
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            raise AgoraError(
                f"add_class_method: file {rel!r} does not exist. "
                f"Use add_class to create the class first."
            )
        source = path.read_text(encoding="utf-8")
        from agora.plan.module_editor import upsert_class_method

        try:
            new_source = upsert_class_method(source, class_name, code)
        except ValueError as exc:
            raise AgoraError(f"add_class_method: {exc}") from exc
        except SyntaxError as exc:
            raise AgoraError(
                f"add_class_method: invalid python. Details: {exc}"
            ) from exc
        # Sprint 7.6(g): same validator as fill_test_body. Implementer
        # method bodies sometimes call `self.some_method_that_doesnt_exist`
        # or similar — reject at tool boundary.
        _reject_unknown_methods_against_spec(ctx, new_source, "add_class_method")
        path.write_text(new_source, encoding="utf-8")
        _record_write(ctx, rel, "add_class_method")
        import ast as _ast
        tree = _ast.parse(code)
        method_name = next(
            (n.name for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))),
            "?",
        )
        return f"upserted method {class_name}.{method_name} in {rel}"

    return add_class_method


def _make_fill_test_body(ctx: ToolContext, role: AgentRole = AgentRole.TESTER):
    async def fill_test_body(args: dict[str, Any]) -> str:
        # Path comes from context — framework binds it during fill_assertions
        # stages. Hidden from manifest when not bound (see get_tool_definitions).
        rel = ctx.active_test_file
        if not rel:
            raise AgoraError(
                "fill_test_body: no active test file bound on context. "
                "This tool is only usable during a fill_assertions stage."
            )
        _enforce_path_scope(role, rel, "fill_test_body")
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            raise AgoraError(f"fill_test_body: bound file not found: {rel}")
        test_name = args.get("test_name", "")
        body_code = args.get("body_code", "")
        if not test_name or not isinstance(test_name, str):
            raise AgoraError("fill_test_body: test_name must be a non-empty string")
        if not body_code or not isinstance(body_code, str):
            raise AgoraError("fill_test_body: body_code must be a non-empty string")

        # Delegate the actual surgery to the pure helper — isolates AST
        # work from I/O and keeps the tool trivially testable.
        from agora.plan.test_scaffolder import replace_test_body

        source = path.read_text(encoding="utf-8")
        try:
            new_source = replace_test_body(source, test_name, body_code)
        except ValueError as exc:
            # User-correctable errors — surface the message.
            raise AgoraError(f"fill_test_body: {exc}") from exc
        except SyntaxError as exc:
            raise AgoraError(
                f"fill_test_body: your body_code produced invalid python. "
                f"Remove any nested `def`, fix any string escapes, and retry. "
                f"Details: {exc}"
            ) from exc

        # v2.7 Sprint 7.6(g): validate method names against the shared
        # api_spec. The whole POINT of api_spec is that tester + implementer
        # can't disagree on API; but 7B kept hallucinating method names
        # inside test bodies (wrote ``URLShortener().add(url)`` when spec
        # declared ``add_url``). Reject-at-tool-boundary forces the model
        # to pick from the authoritative method list.
        _reject_unknown_methods_against_spec(ctx, new_source, "fill_test_body")

        # v2.9 Phase 2: structural L1↔L2 diff. Verify the test body
        # uses method return values in ways consistent with the spec's
        # declared return types. Catches the 2026-04-22 failure where
        # GPT-4o-mini wrote ``mappings[0]['hash']`` on a ``list[str]``
        # return — a certain type violation the tester can correct in
        # one retry with the structured error message.
        #
        # Permissive mode by default: only CERTAIN violations reject;
        # unresolved types (due to missing annotations etc.) are
        # allowed through silently. Flip via AGORA_STRUCTURE_STRICT=1
        # to stress-test for currently-ignored drift.
        _reject_return_type_drift(ctx, new_source, "fill_test_body")

        path.write_text(new_source, encoding="utf-8")
        _record_write(ctx, rel, "fill_test_body")
        return f"filled body of {test_name} in {rel}"

    return fill_test_body


def _method_body_is_stub(node: Any) -> bool:
    """True when a method's body is just ``raise NotImplementedError``
    (possibly with a docstring). Used by the retry-wipe guard on
    :func:`_make_add_class` to decide whether a class already has real
    content worth protecting."""
    import ast as _ast

    body = node.body or []
    # Skip a docstring if present.
    if body and isinstance(body[0], _ast.Expr) and isinstance(body[0].value, _ast.Constant):
        body = body[1:]
    if len(body) != 1:
        return False
    only = body[0]
    if isinstance(only, _ast.Raise) and isinstance(only.exc, _ast.Name):
        return only.exc.id == "NotImplementedError"
    if isinstance(only, _ast.Raise) and isinstance(only.exc, _ast.Call):
        f = only.exc.func
        if isinstance(f, _ast.Name) and f.id == "NotImplementedError":
            return True
    return False


def _load_spec_symbols(ctx: ToolContext) -> tuple[set[str], set[str]] | None:
    """Read ``plan/api_spec.md`` and return ``(class_names, function_names)``.

    Returns ``None`` when no spec is present — callers treat that as
    "no restrictions" and skip validation (back-compat with pre-7.5 plans).
    """
    from pathlib import Path as _P

    spec_path = _P(ctx.work_dir) / "plan" / "api_spec.md"
    if not spec_path.is_file():
        return None
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError:
        return None
    from agora.plan.api_spec import parse_api_spec

    modules = parse_api_spec(spec_text)
    if not modules:
        return None
    classes: set[str] = set()
    functions: set[str] = set()
    for module in modules:
        classes.update(c.name for c in module.classes)
        functions.update(f.name for f in module.functions)
    return classes, functions


def _reject_class_not_in_spec(
    ctx: ToolContext, class_name: str, tool_label: str
) -> None:
    """Reject attempts to create classes that aren't in ``plan/api_spec.md``.

    Sprint 7.7(h) — observed failure: implementer kept creating parallel
    off-spec classes (``ShortUrlRepository``, ``ShortURLRepository``,
    ``UrlShortener``) alongside the spec's ``URLShortener`` and never
    filled in the spec class's methods. Contract tests then failed with
    ``AttributeError`` because the spec class was empty.

    If the spec declares classes at all, :func:`add_class` is restricted
    to those names. If the spec has zero classes, :func:`add_class` is
    banned entirely (the implementer's only job is to fill existing
    stubs — no new classes allowed).
    """
    symbols = _load_spec_symbols(ctx)
    if symbols is None:
        return
    spec_classes, _ = symbols
    if class_name in spec_classes:
        return
    if not spec_classes:
        raise AgoraError(
            f"{tool_label}: the api_spec declares no classes, so you cannot "
            f"create class {class_name!r}. The framework already wrote src/ "
            f"stubs for every symbol in the spec — use add_function to fill "
            f"top-level function bodies, or (if the stub includes a class) "
            f"add_class_method with the spec's class_name."
        )
    raise AgoraError(
        f"{tool_label}: class {class_name!r} is NOT in plan/api_spec.md "
        f"(spec classes: {sorted(spec_classes)}). The framework seeded src/ "
        f"stubs for every spec class at kickoff — do not create parallel "
        f"classes. Use add_class_method(class_name=<one of the spec names>) "
        f"to add methods to the existing stub."
    )


def _reject_function_not_in_spec(
    ctx: ToolContext, func_name: str, tool_label: str
) -> None:
    """Reject top-level functions not in ``plan/api_spec.md``.

    Sprint 7.7(i). Complements :func:`_reject_class_not_in_spec`.
    Permissive on dunder-prefixed names (``_helper``) so implementers
    can add internal helpers, but public top-level functions must match
    the spec exactly.
    """
    if func_name.startswith("_"):
        return
    symbols = _load_spec_symbols(ctx)
    if symbols is None:
        return
    _, spec_functions = symbols
    if func_name in spec_functions:
        return
    if not spec_functions:
        raise AgoraError(
            f"{tool_label}: the api_spec declares no top-level functions "
            f"(only classes), so you cannot create top-level function "
            f"{func_name!r}. If it's a method, pass code with a self-first "
            f"signature and the framework will auto-route to add_class_method. "
            f"For internal helpers, prefix the name with '_'."
        )
    raise AgoraError(
        f"{tool_label}: function {func_name!r} is NOT in plan/api_spec.md "
        f"(spec functions: {sorted(spec_functions)}). Pick from the spec, "
        f"or prefix with '_' for internal helpers."
    )


def _reject_method_not_in_spec(
    ctx: ToolContext, class_name: str, method_name: str, tool_label: str
) -> None:
    """Reject attempts to define methods on spec classes where the method
    name isn't in the spec's method list for that class.

    Sprint 7.7 follow-up: observed failure (sprint77 run) — the implementer
    defined ``URLShortener.save_mapping`` + ``URLShortener.get_mapping`` —
    neither in the spec (spec has ``add_url``/``lookup_hash``). Class name
    validated, but method name slipped through. Contract tests then failed
    with ``AttributeError`` because the spec methods never got defined.

    Permissive on:
      - ``_`` prefix — internal helpers are legitimate.
      - Dunder methods (``__init__`` / ``__eq__`` / ``__repr__``) — framework
        boilerplate the spec usually doesn't list explicitly.
    """
    if method_name.startswith("_"):
        return  # private + dunder
    symbols = _load_spec_symbols(ctx)
    if symbols is None:
        return
    # Re-parse the spec to get methods per class (_load_spec_symbols flattens).
    from pathlib import Path as _P
    spec_path = _P(ctx.work_dir) / "plan" / "api_spec.md"
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError:
        return
    from agora.plan.api_spec import parse_api_spec

    modules = parse_api_spec(spec_text)
    for module in modules:
        for cls in module.classes:
            if cls.name != class_name:
                continue
            spec_methods = {m.name for m in cls.methods}
            if method_name in spec_methods:
                return
            raise AgoraError(
                f"{tool_label}: method {class_name}.{method_name!r} is NOT in "
                f"plan/api_spec.md (spec methods for {class_name}: "
                f"{sorted(spec_methods)}). Pick from the spec, or prefix "
                f"with '_' for internal helpers. The tests import "
                f"{class_name} and call the spec's method names — defining "
                f"different names will cause AttributeError at test time."
            )


def _reject_unknown_methods_against_spec(
    ctx: ToolContext, source: str, tool_label: str
) -> None:
    """Raise :class:`AgoraError` if ``source`` calls methods on api_spec
    classes that don't exist in the spec.

    Reads ``plan/api_spec.md`` from the task's work_dir; no-op when the
    spec file is missing (pre-7.5 runs and tests-of-tests). Reuses
    :func:`agora.plan.api_spec.find_unknown_method_calls`.
    """
    from pathlib import Path as _P

    spec_path = _P(ctx.work_dir) / "plan" / "api_spec.md"
    if not spec_path.is_file():
        return
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError:
        return
    from agora.plan.api_spec import find_unknown_method_calls, parse_api_spec

    modules = parse_api_spec(spec_text)
    if not modules:
        return
    violations = find_unknown_method_calls(source, modules)
    if not violations:
        return
    preview = "; ".join(violations[:3])
    more = f" (+{len(violations) - 3} more)" if len(violations) > 3 else ""
    raise AgoraError(
        f"{tool_label}: your code calls methods that are NOT in the "
        f"api_spec — {preview}{more}. Pick from the known methods "
        f"listed. The spec at plan/api_spec.md is authoritative."
    )


def _reject_return_type_drift(
    ctx: ToolContext, source: str, tool_label: str
) -> None:
    """v2.9 Phase 2 gate: raise :class:`AgoraError` when a test body uses
    method return values in ways inconsistent with the spec's declared
    return types.

    Example: spec declares ``list_mappings() -> list[str]``; test does
    ``list_mappings()[0]['key']``. Subscripting a ``str`` with a string
    key is a certain type violation — the test would never pass, no
    matter what the implementer wrote. Catching it at tester-write time
    saves the impl's retry budget.

    Permissive mode (default): only ``certain`` violations raise.
    Strict mode (``AGORA_STRUCTURE_STRICT=1``): also ``unresolved`` ones
    (for stress-testing / debugging).

    No-op when the spec file is missing, empty, or can't be parsed.
    """
    import os
    from pathlib import Path as _P

    spec_path = _P(ctx.work_dir) / "plan" / "api_spec.md"
    if not spec_path.is_file():
        return
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError:
        return

    from agora.plan.structure import (
        Mode,
        check_usage_matches_contract,
        extract_contract,
        extract_usage_traces,
        filter_by_mode,
    )

    contract = extract_contract(spec_text)
    if not contract.modules:
        return
    traces = extract_usage_traces(source, contract)
    if not traces:
        return
    all_violations = check_usage_matches_contract(traces, contract)
    mode = (
        Mode.STRICT
        if os.getenv("AGORA_STRUCTURE_STRICT", "").lower() in ("1", "true", "yes")
        else Mode.PERMISSIVE
    )
    violations = filter_by_mode(all_violations, mode)
    if not violations:
        return
    lines = [
        f"{tool_label}: test body uses method return values in ways "
        f"inconsistent with the api_spec's declared return types.",
    ]
    for v in violations[:5]:
        lines.append(f"  - {v.path}")
        lines.append(f"    {v.message}")
    if len(violations) > 5:
        lines.append(f"  (+{len(violations) - 5} more)")
    lines.append(
        "fix: adjust the test body to match the spec's return types. "
        "If the spec's return type is wrong for what the brief needs, "
        "the spec must be re-authored FIRST (it's the shared contract "
        "between tester and implementer)."
    )
    raise AgoraError("\n".join(lines))


def _make_edit_insert_before(ctx: ToolContext, role: AgentRole = AgentRole.ARCHITECT):
    async def edit_file_insert_before(args: dict[str, Any]) -> str:
        rel = args["path"]
        _enforce_path_scope(role, rel, "edit_file_insert_before")
        _reject_edit_on_stub(ctx, rel, "edit_file_insert_before")
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            return f"ERROR: file not found: {rel}"
        anchor = args["anchor"]
        snippet = args["snippet"]
        if not anchor:
            raise AgoraError("edit_file_insert_before: anchor must not be empty")
        body = path.read_text(encoding="utf-8")
        lines = body.splitlines(keepends=True)
        matches = [i for i, line in enumerate(lines) if anchor in line]
        if not matches:
            raise AgoraError(
                f"edit_file_insert_before: anchor {anchor!r} not found in {rel}. "
                f"read_file to see current content and pick a substring of one line."
            )
        if len(matches) > 1:
            locations = _format_match_locations(body, anchor)
            raise AgoraError(
                f"edit_file_insert_before: anchor {anchor!r} appears in "
                f"{len(matches)} lines of {rel}; must be unique.\n\n"
                f"Match locations:\n{locations}\n\n"
                f"Pick a longer substring that uniquely identifies ONE of the "
                f"lines above — e.g. include neighboring characters or combine "
                f"the anchor with an adjacent token visible in the context."
            )
        idx = matches[0]
        snippet_block = snippet if snippet.endswith("\n") else snippet + "\n"
        new_lines = lines[:idx] + [snippet_block] + lines[idx:]
        path.write_text("".join(new_lines), encoding="utf-8")
        _record_write(ctx, rel, "edit_file_insert_before")
        return f"inserted {len(snippet_block)} chars before {anchor!r} in {rel}"

    return edit_file_insert_before


def _make_edit_append(ctx: ToolContext, role: AgentRole = AgentRole.ARCHITECT):
    async def edit_file_append(args: dict[str, Any]) -> str:
        rel = args["path"]
        _enforce_path_scope(role, rel, "edit_file_append")
        _reject_edit_on_stub(ctx, rel, "edit_file_append")
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            return f"ERROR: file not found: {rel}"
        snippet = args["snippet"]
        body = path.read_text(encoding="utf-8")
        # Ensure a newline separator so the appended block doesn't glue onto
        # the last line of the existing file.
        if body and not body.endswith("\n"):
            body += "\n"
        new_body = body + (snippet if snippet.endswith("\n") else snippet + "\n")
        path.write_text(new_body, encoding="utf-8")
        _record_write(ctx, rel, "edit_file_append")
        return f"appended {len(snippet)} chars to {rel}"

    return edit_file_append


def _make_write(ctx: ToolContext, role: AgentRole = AgentRole.ARCHITECT):
    """write_file is for CREATING files. Non-empty files can't be clobbered.

    Enforces the authoring discipline: first write is free, subsequent
    modifications go through ``edit_file_replace``. Catches the 7B failure
    mode where the model re-calls write_file with a truncated version of
    its own prior output (observed: a 131-byte function definition got
    overwritten by a 36-byte import-only stub on the very next turn).

    Rules:
      - File doesn't exist yet → write OK.
      - File exists but is empty (size 0) → write OK (placeholder replacement).
      - File exists AND has content → refuse; tell model to use
        ``edit_file_replace`` instead. ``force=true`` bypasses the guard
        for genuine full-rewrites.
    """

    async def write_file(args: dict[str, Any]) -> str:
        rel = args["path"]
        _enforce_path_scope(role, rel, "write_file")
        path = _safe_path(ctx.work_dir, rel)
        force = bool(args.get("force", False))
        if path.is_file() and path.stat().st_size > 0 and not force:
            existing = path.stat().st_size
            # Flip the per-task guard so subsequent turns don't see write_file
            # at all — the 7B otherwise keeps calling it on auxiliary paths
            # (README, requirements.txt) and burns iterations cycling.
            ctx.write_file_blocked = True
            return (
                f"ERROR: {rel!r} already exists with {existing} bytes of "
                f"content. write_file has been disabled for the rest of this "
                f"task — use edit_file_replace, edit_file_insert_before, or "
                f"edit_file_append to modify files that already exist."
            )
        # v2.4: refuse module/package stem collisions. Writing src/foo.py
        # when src/foo/ exists (or vice versa) produces an import trap:
        # Python prefers the package but the author meant the module.
        # Catching it at write-time forces the model to pick one.
        sibling = _stem_collision(path)
        if sibling is not None and not force:
            return (
                f"ERROR: writing {rel!r} would collide with the existing "
                f"{sibling!s} — Python's import system would resolve "
                f"imports to the other one. Pick ONE: either delete_file "
                f"the existing sibling first, or write to a different name."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

        # v2.9 Phase 2+ (auto-heal): when the architect writes
        # plan/api_spec.md and includes ``## module: src/tests/...`` or
        # ``## module: tests/...`` sections, strip them BEFORE writing.
        # Test modules are never valid in api_spec, and GPT-4o-mini has
        # been observed to persistently include them across 3 retries
        # despite the instruction AND the C5 validator rejecting them.
        # Stripping at write-time is safe (content is always invalid if
        # present) and saves the architect's retry budget.
        content = args["content"]
        extra_msg = ""
        rel_norm = rel.replace("\\", "/")
        if rel_norm == "plan/api_spec.md":
            from agora.plan.api_spec import strip_test_module_sections

            cleaned, removed = strip_test_module_sections(content)
            if removed:
                content = cleaned
                logger.warning(
                    "write_file auto-strip: removed test module section(s) "
                    "from plan/api_spec.md: %s",
                    removed,
                )
                extra_msg = (
                    f" (framework auto-stripped test module section(s) "
                    f"{removed} — api_spec is for production modules only; "
                    f"tests are scaffolded separately by the test pipeline)"
                )

        path.write_text(content, encoding="utf-8")
        if rel not in ctx.written_files:
            ctx.written_files.append(rel)
        if ctx.expected_output_path and rel != ctx.expected_output_path:
            logger.warning(
                "write_file path mismatch: wrote to %r but task expected %r",
                rel, ctx.expected_output_path,
            )
        return f"wrote {len(content)} bytes to {rel}{extra_msg}"

    return write_file


def _make_list(ctx: ToolContext):
    async def list_directory(args: dict[str, Any]) -> str:
        path = _safe_path(ctx.work_dir, args.get("path", "."))
        if not path.is_dir():
            return f"ERROR: not a directory: {args.get('path', '.')}"
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    return list_directory


def _make_delete_file(ctx: ToolContext, role: AgentRole = AgentRole.ARCHITECT):
    """Remove a file or empty directory. Scoped narrowly to avoid accidental
    data loss: non-empty directories are refused; the caller must explicitly
    remove contents first. Intended for unblocking module/package stem
    collisions (src/foo.py + src/foo/) where one must go."""

    async def delete_file(args: dict[str, Any]) -> str:
        rel = args.get("path")
        if not rel:
            return "ERROR: path is required"
        _enforce_path_scope(role, rel, "delete_file")
        path = _safe_path(ctx.work_dir, rel)
        if not path.exists():
            return f"ERROR: {rel!r} does not exist"
        try:
            if path.is_dir():
                # Refuse non-empty directories — the model should be
                # deliberate about what it's removing.
                if any(path.iterdir()):
                    return (
                        f"ERROR: {rel!r} is a non-empty directory; "
                        f"delete its contents first with delete_file on each entry"
                    )
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            return f"ERROR: delete_file failed: {exc}"
        # Drop the path from written_files tracking so a subsequent write_file
        # can recreate it without tripping the overwrite guard.
        if rel in ctx.written_files:
            ctx.written_files.remove(rel)
        return f"deleted {rel}"

    return delete_file


def _make_check_python(ctx: ToolContext):
    """Compile-check + module-scope undefined-name check + post-__main__
    unreachable-code check. Does not execute.
    """
    import py_compile

    async def check_python(args: dict[str, Any]) -> str:
        path = _safe_path(ctx.work_dir, args["path"])
        if not path.is_file():
            return f"ERROR: file not found: {args['path']}"
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            return f"SyntaxError in {args['path']}:\n{exc.msg.strip()}"
        except SyntaxError as exc:
            return f"SyntaxError in {args['path']} at line {exc.lineno}: {exc.msg}"
        source = path.read_text(encoding="utf-8")
        undefined = _find_module_scope_undefined_names(source)
        if undefined:
            lines = [f"undefined name(s) in {args['path']} (module scope):"]
            for name, lineno in undefined:
                lines.append(f"  - '{name}' at line {lineno}")
            return "\n".join(lines)
        stragglers = _find_code_after_main_block(source)
        if stragglers:
            lines = [
                f"unreachable code in {args['path']} — module-scope statements "
                "after `if __name__ == '__main__':` never execute at runtime "
                "(the __main__ block blocks, stdlib imports below it never run):"
            ]
            for kind, lineno in stragglers:
                lines.append(f"  - {kind} at line {lineno}")
            return "\n".join(lines)
        return f"OK: {args['path']}"

    return check_python


def _find_module_scope_undefined_names(source: str) -> list[tuple[str, int]]:
    """Return ``[(name, lineno), ...]`` for module-scope Loads with no binding.

    Two-pass AST walk that **only** inspects top-level statements (never
    descends into function, async-function, or class bodies). Keeps the check
    cheap and false-positive-free: function locals are irrelevant, and a real
    scope-aware checker (pyflakes) is out of scope.
    """
    import ast
    import builtins

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    defined: set[str] = set(vars(builtins).keys())
    defined.update({
        "__name__", "__file__", "__doc__", "__all__",
        "__spec__", "__loader__", "__package__", "__builtins__",
    })

    for node in tree.body:
        _collect_module_scope_bindings(node, defined)

    undefined: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for node in tree.body:
        for name, lineno in _iter_module_scope_loads(node):
            if name in defined:
                continue
            key = (name, lineno)
            if key in seen:
                continue
            seen.add(key)
            undefined.append(key)
    return undefined


def _collect_module_scope_bindings(node: "ast.stmt", defined: "set[str]") -> None:
    """Populate ``defined`` with every name that ``node`` (module-scope) binds.

    Recurses into if/try/for/with bodies (those share module scope) but not
    into function/class bodies.
    """
    import ast

    if isinstance(node, ast.Import):
        for alias in node.names:
            defined.add(alias.asname or alias.name.split(".", 1)[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            defined.add(alias.asname or alias.name)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            _unpack_assign_target(target, defined)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        _unpack_assign_target(node.target, defined)
    elif isinstance(node, (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                _collect_module_scope_bindings(child, defined)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            _unpack_assign_target(node.target, defined)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _unpack_assign_target(item.optional_vars, defined)
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.name:
                    defined.add(handler.name)


def _unpack_assign_target(target: "ast.expr", defined: "set[str]") -> None:
    import ast

    if isinstance(target, ast.Name):
        defined.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _unpack_assign_target(elt, defined)
    elif isinstance(target, ast.Starred):
        _unpack_assign_target(target.value, defined)


def _iter_module_scope_loads(node: "ast.stmt"):
    """Yield ``(name, lineno)`` for every Name-Load that executes at module scope.

    Skips function and class bodies (those have their own scopes). Still yields
    loads that appear in decorators and class/function default args, since
    those are evaluated when the def itself runs — i.e. at module scope.
    """
    import ast

    def _walk(n: "ast.AST"):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            yield n.id, n.lineno
            return
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in n.decorator_list:
                yield from _walk(dec)
            for default in n.args.defaults:
                yield from _walk(default)
            for default in n.args.kw_defaults:
                if default is not None:
                    yield from _walk(default)
            for ann in _iter_annotations(n.args):
                yield from _walk(ann)
            if n.returns is not None:
                yield from _walk(n.returns)
            return
        if isinstance(n, ast.ClassDef):
            for dec in n.decorator_list:
                yield from _walk(dec)
            for base in n.bases:
                yield from _walk(base)
            for kw in n.keywords:
                yield from _walk(kw.value)
            return
        if isinstance(n, ast.Lambda):
            return
        for child in ast.iter_child_nodes(n):
            yield from _walk(child)

    yield from _walk(node)


def _iter_annotations(args: "ast.arguments"):
    import ast

    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.annotation is not None:
            yield arg.annotation
    if args.vararg is not None and args.vararg.annotation is not None:
        yield args.vararg.annotation
    if args.kwarg is not None and args.kwarg.annotation is not None:
        yield args.kwarg.annotation


def _find_code_after_main_block(source: str) -> list[tuple[str, int]]:
    """Return module-scope statements that appear AFTER ``if __name__ == '__main__':``.

    Motivation: a handler block (e.g. ``@bot.tree.command def roll(...)``)
    placed after ``bot.run(TOKEN)`` registers at *import* time (tests see it)
    but never executes at runtime (``bot.run`` blocks). The result is a
    handler that tests pretend exists but production never registers. Run 13
    shipped exactly this bug.

    We detect the module-scope ``if __name__ == '__main__':`` node and flag
    every subsequent top-level statement. Returns ``[(kind, lineno), ...]``
    where ``kind`` is a short description ("function def 'roll'",
    "import 'random'", "expression").
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    main_idx: int | None = None
    for i, node in enumerate(tree.body):
        if _is_if_name_equals_main(node):
            main_idx = i
            break
    if main_idx is None:
        return []

    stragglers: list[tuple[str, int]] = []
    for node in tree.body[main_idx + 1 :]:
        stragglers.append((_describe_statement(node), node.lineno))
    return stragglers


def _is_if_name_equals_main(node: "Any") -> bool:
    """Is ``node`` exactly ``if __name__ == '__main__':`` at module scope?"""
    import ast

    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if len(test.comparators) != 1:
        return False
    left, right = test.left, test.comparators[0]
    # Accept either order: `__name__ == '__main__'` or `'__main__' == __name__`.
    if _is_dunder_name(left) and _is_main_literal(right):
        return True
    if _is_dunder_name(right) and _is_main_literal(left):
        return True
    return False


def _is_dunder_name(node: "Any") -> bool:
    import ast

    return isinstance(node, ast.Name) and node.id == "__name__"


def _is_main_literal(node: "Any") -> bool:
    import ast

    return isinstance(node, ast.Constant) and node.value == "__main__"


def _describe_statement(node: "Any") -> str:
    """Short human-readable label for a module-scope statement."""
    import ast

    if isinstance(node, ast.FunctionDef):
        return f"function def {node.name!r}"
    if isinstance(node, ast.AsyncFunctionDef):
        return f"async function def {node.name!r}"
    if isinstance(node, ast.ClassDef):
        return f"class def {node.name!r}"
    if isinstance(node, ast.Import):
        names = ", ".join(a.name for a in node.names)
        return f"import {names}"
    if isinstance(node, ast.ImportFrom):
        mod = node.module or "?"
        names = ", ".join(a.name for a in node.names)
        return f"from {mod} import {names}"
    if isinstance(node, ast.Assign):
        targets = ", ".join(
            t.id for t in node.targets if isinstance(t, ast.Name)
        )
        return f"assignment to {targets!r}" if targets else "assignment"
    if isinstance(node, ast.If):
        return "if-block"
    if isinstance(node, ast.Expr):
        return "expression"
    return type(node).__name__


# ---- runtime ----


def _make_run_python_import(ctx: ToolContext):
    """Import a module under work_dir in a subprocess. Catches eager failures."""
    import asyncio

    from agora.fleet._subprocess import format_failure, run_host_python

    def _blocking(path: Path, rel: str) -> str:
        # Escape backslashes so the inline literal parses on Windows paths.
        literal = str(path).replace("\\", "\\\\").replace("'", "\\'")
        probe = (
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('__probe__', '{literal}')\n"
            "if spec is None or spec.loader is None:\n"
            "    raise ImportError('could not build import spec')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print('OK')\n"
        )
        result = run_host_python(["-c", probe], cwd=ctx.work_dir, timeout=15.0)
        if result.ok:
            return f"OK: imported {rel}"
        return f"IMPORT FAILED for {rel}\n{format_failure(result, 1800)}"

    async def run_python_import(args: dict[str, Any]) -> str:
        rel = args["path"]
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            return f"ERROR: file not found: {rel}"
        return await asyncio.to_thread(_blocking, path, rel)

    return run_python_import


def _make_run_pytest(ctx: ToolContext):
    """Run pytest against a file or directory under work_dir."""
    import asyncio

    from agora.fleet._subprocess import format_failure, run_host_python

    def _blocking(rel: str, maxfail: int) -> str:
        result = run_host_python(
            [
                "-m", "pytest", rel,
                "-x", f"--maxfail={maxfail}",
                "-q", "--tb=short", "--no-header",
                "-p", "no:cacheprovider",
            ],
            cwd=ctx.work_dir,
            timeout=60.0,
        )
        if result.ok:
            last = [ln for ln in result.stdout.splitlines() if ln.strip()]
            summary = last[-1] if last else "passed"
            return f"OK: {summary}"
        return f"PYTEST FAILED for {rel}\n{format_failure(result, 2200)}"

    async def run_pytest(args: dict[str, Any]) -> str:
        rel = str(args.get("path", "."))
        maxfail = int(args.get("maxfail", 1))
        # For ``rel == "."`` we skip the file-exists check; pytest handles it.
        if rel not in (".", "") and not _safe_path(ctx.work_dir, rel).exists():
            return f"ERROR: not found: {rel}"
        return await asyncio.to_thread(_blocking, rel, maxfail)

    return run_pytest


def _make_check_requirements(ctx: ToolContext):
    """Parse ``requirements.txt`` against PEP 508 (via ``packaging``)."""

    async def check_requirements(args: dict[str, Any]) -> str:
        rel = str(args.get("path", "requirements.txt"))
        path = _safe_path(ctx.work_dir, rel)
        if not path.is_file():
            return f"ERROR: file not found: {rel}"
        try:
            from packaging.requirements import InvalidRequirement, Requirement
        except ImportError:
            return f"OK: {rel} (packaging not available, skipped)"
        errors: list[str] = []
        total = 0
        for i, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            total += 1
            try:
                Requirement(line)
            except InvalidRequirement as exc:
                errors.append(f"line {i}: {raw!r}: {exc}")
        if errors:
            preview = "\n  ".join(errors[:5])
            return f"REQUIREMENTS INVALID for {rel}:\n  {preview}"
        return f"OK: {rel} ({total} requirements)"

    return check_requirements


# ---- research ----


def _make_search(ctx: ToolContext):
    async def web_search(args: dict[str, Any]) -> str:
        if ctx.search_fn is None:
            return "ERROR: web_search is not enabled in this context"
        return await ctx.search_fn(args["query"])

    return web_search


def _make_fetch(ctx: ToolContext):
    async def fetch_url(args: dict[str, Any]) -> str:
        if ctx.fetch_fn is None:
            return "ERROR: fetch_url is not enabled in this context"
        url = args["url"]
        save_as = args.get("save_as")
        content = await ctx.fetch_fn(url)
        if not save_as:
            return content
        rel = str(save_as)
        path = _safe_path(ctx.work_dir, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if rel not in ctx.written_files:
            ctx.written_files.append(rel)
        if ctx.expected_output_path and rel != ctx.expected_output_path:
            logger.warning(
                "fetch_url save_as path mismatch: saved to %r but task expected %r",
                rel, ctx.expected_output_path,
            )
        return f"fetched {len(content)} chars to {rel}"

    return fetch_url


def _make_recall_knowledge(ctx: ToolContext):
    """Local keyword-match retrieval over uploaded knowledge refs.

    ``ctx.knowledge_fetcher`` is responsible for translating an ``mxc://`` URI
    to a local file path (the orchestrator wires this based on the identity's
    ``knowledge_refs``). If no fetcher is configured, the tool reports an error.
    """
    from pathlib import Path

    async def recall_knowledge(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: query is required"
        if not ctx.knowledge_refs:
            return "(no knowledge documents attached to this agent)"
        if ctx.knowledge_fetcher is None:
            return "ERROR: knowledge fetcher not configured"

        terms = [t.lower() for t in query.split() if t.strip()]
        hits: list[tuple[int, str, int, str]] = []  # score, filename, lineno, line
        for mxc in ctx.knowledge_refs:
            try:
                local = await ctx.knowledge_fetcher(mxc)
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: fetching {mxc}: {exc}"
            path = Path(local)
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, start=1):
                lowered = line.lower()
                score = sum(lowered.count(term) for term in terms)
                if score > 0:
                    hits.append((score, path.name, i, line.strip()))

        if not hits:
            return f"(no matches for {query!r} in {len(ctx.knowledge_refs)} document(s))"
        hits.sort(key=lambda h: h[0], reverse=True)
        top = hits[:3]
        return "\n".join(
            f"[{name}:{lineno}] {text}" for _score, name, lineno, text in top
        )

    return recall_knowledge


# ---- git (Sprint 4 wires in the real RepoManager) ----


def _make_git_commit(ctx: ToolContext):
    async def git_commit(args: dict[str, Any]) -> str:
        if ctx.git_repo is None:
            return "ERROR: no git repo configured"
        return str(ctx.git_repo.commit_all(args["message"]))

    return git_commit


def _make_git_diff(ctx: ToolContext):
    async def git_diff(_args: dict[str, Any]) -> str:
        if ctx.git_repo is None:
            return "ERROR: no git repo configured"
        return str(ctx.git_repo.diff())

    return git_diff


def _make_git_log(ctx: ToolContext):
    async def git_log(args: dict[str, Any]) -> str:
        if ctx.git_repo is None:
            return "ERROR: no git repo configured"
        return str(ctx.git_repo.log(limit=int(args.get("limit", 10))))

    return git_log


# ---- coordination ----


def _make_report_progress(ctx: ToolContext):
    async def report_progress(args: dict[str, Any]) -> str:
        entry = {"message": args["message"]}
        ctx.progress_log.append(entry)
        await ctx.matrix_client.send_event(
            ctx.project_room_id, "m.agora.progress", entry
        )
        return "progress reported"

    return report_progress


def _make_request_review(ctx: ToolContext):
    """Rate-limited to one review per task. 7B models spam-call request_review
    on every turn after completing minor sub-steps (observed: 5 review polls
    fired in 90s during setup_project), flooding the project room with
    unread indicators. The first call posts a Matrix event; subsequent calls
    in the same task no-op with a hint to call mark_complete instead."""

    async def request_review(args: dict[str, Any]) -> str:
        if ctx.reviews_requested:
            return (
                "review already requested for this task — call mark_complete "
                "when done, or stop calling tools and let the stage finish."
            )
        entry = {"summary": args["summary"]}
        ctx.reviews_requested.append(entry)
        await ctx.matrix_client.send_event(
            ctx.project_room_id, "m.agora.review_request", entry
        )
        return "review requested"

    return request_review


def _make_mark_complete(ctx: ToolContext):
    async def mark_complete(args: dict[str, Any]) -> str:
        entry = {
            "summary": args["summary"],
            "artifacts": list(args.get("artifacts", [])),
        }
        ctx.completions.append(entry)
        return json.dumps(entry)

    return mark_complete


def _make_report_learning(ctx: ToolContext):
    async def report_learning(args: dict[str, Any]) -> str:
        entry = {
            "category": args["category"],
            "content": args["content"],
            "confidence": float(args.get("confidence", 0.6)),
        }
        ctx.reported_learnings.append(entry)
        return "learning recorded"

    return report_learning


def _make_post_note(ctx: ToolContext):
    """Tool executor: post an ``m.room.message`` to the project room.

    Minimal surface so planner tasks can make intermediate artifacts visible
    to the user in Element before gating on ``await_user_decision``.
    """

    async def post_note(args: dict[str, Any]) -> str:
        body = str(args.get("body", "")).strip()
        if not body:
            # Self-teaching error: weak models confuse post_note with
            # mark_complete (args: summary, artifacts) or write_file
            # (args: path, content). List what they sent + what's correct
            # so the next turn can recover.
            got = sorted(args.keys()) or ["<none>"]
            return (
                f"ERROR: post_note requires ONE argument: body (a string). "
                f"Got: {got}. "
                "If you want to mark task completion, call mark_complete "
                "(args: summary, artifacts). If you want to write a file, "
                "call write_file (args: path, content)."
            )
        try:
            await ctx.matrix_client.send_event(
                ctx.project_room_id,
                "m.room.message",
                {"msgtype": "m.text", "body": body},
            )
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: post_note failed: {exc}"
        return "note posted"

    return post_note


def _make_await_user_decision(ctx: ToolContext):
    """Tool executor: post a Matrix poll, BLOCK on a future, return chosen answer.

    Requires ``ctx.control`` (the :class:`OrchestratorControl` for this project)
    and a connected ``matrix_client``. Routing is: tool sends poll →
    orchestrator's decision-poll handler catches the response →
    ``control.resolve_decision`` completes the future → this awaits returns.

    Timeouts are enforced by :meth:`OrchestratorControl.await_decision` (default
    300s, override via the ``timeout_seconds`` argument).
    """
    import asyncio

    async def await_user_decision(args: dict[str, Any]) -> str:
        control = ctx.control
        if control is None:
            return "ERROR: await_user_decision requires an observer-enabled project"

        question = str(args["question"])
        decision_id = str(args["decision_id"])
        options_in = args.get("options") or []
        if not isinstance(options_in, list) or len(options_in) < 2:
            return "ERROR: options must be a list of at least 2 answer ids"
        timeout = float(args.get("timeout_seconds", 300.0))

        # Build (answer_id, label) tuples. If the caller passed plain strings,
        # reuse them as both id and label.
        options: list[tuple[str, str]] = []
        for opt in options_in:
            if isinstance(opt, str):
                options.append((opt, opt))
            elif isinstance(opt, dict) and "id" in opt:
                label = str(opt.get("label") or opt["id"])
                options.append((str(opt["id"]), label))
            else:
                return f"ERROR: malformed option entry: {opt!r}"

        # Pre-register the future BEFORE sending the poll so a very fast user
        # click can't land before we're listening.
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        control.pending_decisions[decision_id] = future

        from agora.observe.polls import (
            POLL_START_TYPE,
            build_decision_poll,
        )

        # First: post a plain ``m.room.message`` so every Matrix client (even
        # ones that don't render MSC3381 polls — which some Element builds
        # still skip) sees the question + options + chat-fallback hint. This
        # is what actually makes the decision answerable in practice.
        options_lines = "\n".join(f"  - {aid}: {lbl}" for aid, lbl in options)
        fallback_body = (
            f"**Decision: {decision_id}**\n\n"
            f"{question}\n\n"
            f"Options:\n{options_lines}\n\n"
            f"Vote via the poll below OR type `/agora decision {decision_id} "
            f"<answer_id>` in this room."
        )
        try:
            await ctx.matrix_client.send_event(
                ctx.project_room_id,
                "m.room.message",
                {"msgtype": "m.text", "body": fallback_body},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision-fallback note post failed: %s", exc)

        poll_content = build_decision_poll(question, options, decision_id)
        try:
            poll_event_id = await ctx.matrix_client.send_event(
                ctx.project_room_id, POLL_START_TYPE, poll_content
            )
        except Exception as exc:  # noqa: BLE001
            control.pending_decisions.pop(decision_id, None)
            return f"ERROR: failed to post decision poll: {exc}"

        control.register_decision_poll(decision_id, poll_event_id)

        try:
            answer = await control.await_decision(decision_id, timeout_seconds=timeout)
        except asyncio.TimeoutError:
            return f"ERROR: decision {decision_id!r} timed out after {timeout}s"
        return answer

    return await_user_decision


# ==================================================================
# Plan-authoring executors
# ==================================================================
#
# All six share the same shape: they lazy-init ``ctx.plan_draft`` on first
# call, mutate it via a ``PlanDraft`` method, and return a short string. Any
# validation error raised by :class:`PlanDraft` becomes an ``ERROR: ...``
# result the LLM sees on its next turn — standard tool-feedback loop.


def _plan_draft(ctx: ToolContext):
    """Lazy-init + return the plan draft shared across all tasks in the run.

    The draft lives on ``ctx.control.plan_draft`` (one instance per project)
    so the mutations a task makes via ``plan_add_task`` etc. are visible to
    later tasks — including the ``finalize_plan`` framework stage. If no
    control is attached (single_task mode, tests), we fall back to
    ``ctx.plan_draft`` so the tool still works standalone.
    """
    from agora.plan.builder import PlanDraft

    control = ctx.control
    if control is not None and hasattr(control, "plan_draft"):
        if control.plan_draft is None or not isinstance(control.plan_draft, PlanDraft):
            control.plan_draft = PlanDraft()
        # Mirror onto ctx too so evaluate_postconditions (which reads
        # ``ctx.plan_draft``) sees the same instance in the plan-draft
        # gate predicates.
        ctx.plan_draft = control.plan_draft
        return control.plan_draft

    if ctx.plan_draft is None or not isinstance(ctx.plan_draft, PlanDraft):
        ctx.plan_draft = PlanDraft()
    return ctx.plan_draft


def _make_plan_set_agents(ctx: ToolContext):
    async def plan_set_agents(args: dict[str, Any]) -> str:
        agents = args.get("agents")
        if not isinstance(agents, list):
            return "ERROR: agents must be a list"
        draft = _plan_draft(ctx)
        try:
            draft.set_agents(agents)
        except AgoraError as exc:
            return f"ERROR: {exc}"
        return f"OK: agents set ({len(draft.agents)}: {[a['name'] for a in draft.agents]})"

    return plan_set_agents


def _make_plan_upsert_agent(ctx: ToolContext):
    """Add-or-replace one agent by name. The narrow per-agent author stages
    each call this once; idempotent on name so a retry of the same stage
    updates (instead of duplicates) the prior attempt's entry."""

    async def plan_upsert_agent(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        role = str(args.get("role", "")).strip()
        instructions = str(args.get("instructions", ""))
        model = str(args.get("model", "") or "")
        if not name:
            return "ERROR: name is required"
        if not role:
            return "ERROR: role is required"
        draft = _plan_draft(ctx)
        try:
            added = draft.upsert_agent(name, role, instructions, model)
        except AgoraError as exc:
            return f"ERROR: {exc}"
        verb = "added" if added else "updated"
        return (
            f"OK: agent {name!r} {verb} "
            f"({len(draft.agents)} total: {[a['name'] for a in draft.agents]})"
        )

    return plan_upsert_agent


def _make_plan_add_task(ctx: ToolContext):
    async def plan_add_task(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        description = str(args.get("description", ""))
        assigned_to = str(args.get("assigned_to", "")).strip()
        depends_on = args.get("depends_on") or []
        output_path = str(args.get("output_path", "") or "")
        if not task_id or not assigned_to:
            return "ERROR: task_id and assigned_to are required"
        if not isinstance(depends_on, list):
            return "ERROR: depends_on must be a list of task ids"
        draft = _plan_draft(ctx)
        try:
            draft.add_task(task_id, description, assigned_to, depends_on, output_path)
        except AgoraError as exc:
            return f"ERROR: {exc}"
        return f"OK: task {task_id!r} added ({len(draft.tasks)} total)"

    return plan_add_task


def _load_api_spec_modules(ctx: ToolContext) -> set[str] | None:
    """Read ``plan/api_spec.md`` from ``ctx.work_dir`` and return the set of
    production module paths declared in it.

    Returns None when no work_dir is set or the file doesn't exist yet
    (e.g. author_tasks running before define_api in a test) — callers treat
    None as "skip validation, we have no spec to check against". Returns
    an empty set when the file exists but declares no modules (still skip
    validation; the api_spec_is_valid gate on define_api catches that).
    """
    if not ctx.work_dir:
        return None
    spec_path = Path(ctx.work_dir) / "plan" / "api_spec.md"
    if not spec_path.is_file():
        return None
    try:
        text = spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    from agora.plan.api_spec import parse_api_spec

    modules = parse_api_spec(text)
    # Only production (non-test) modules count as valid targets. Normalise
    # path separators so Windows-authored specs match POSIX-style task paths.
    out: set[str] = set()
    for m in modules:
        norm = m.path.replace("\\", "/")
        if norm.startswith("src/tests/") or norm.startswith("tests/"):
            continue
        if norm.startswith("src/") and norm.endswith(".py"):
            out.add(norm)
    return out


def _make_plan_add_task_spec(ctx: ToolContext):
    async def plan_add_task_spec(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        description = str(args.get("description", ""))
        assigned_to = str(args.get("assigned_to", "")).strip()
        depends_on = args.get("depends_on") or []
        output_path = str(args.get("output_path", "") or "")
        postconditions = args.get("postconditions") or []
        if not task_id or not assigned_to:
            return "ERROR: task_id and assigned_to are required"
        if not isinstance(depends_on, list):
            return "ERROR: depends_on must be a list of task ids"
        if not isinstance(postconditions, list) or not postconditions:
            return (
                "ERROR: postconditions must be a non-empty list "
                "(minimum: [{\"name\": \"mark_complete\"}])"
            )
        draft = _plan_draft(ctx)
        # v2.8(C4a): pull api_spec modules from disk so the draft can
        # validate this task's src/*.py references against the spec the
        # prior define_api task froze. None → no spec file yet (skip check).
        api_spec_modules = _load_api_spec_modules(ctx)
        try:
            attached = draft.add_task_spec(
                task_id,
                description,
                assigned_to,
                depends_on,
                output_path,
                postconditions,
                api_spec_modules=api_spec_modules,
            )
        except AgoraError as exc:
            return f"ERROR: {exc}"
        return (
            f"OK: task {task_id!r} added with {attached} postcondition(s) "
            f"({len(draft.tasks)} tasks total)"
        )

    return plan_add_task_spec


def _make_plan_attach_postcondition(ctx: ToolContext):
    async def plan_attach_postcondition(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        name = str(args.get("name", "")).strip()
        pc_args = args.get("args") or {}
        if not isinstance(pc_args, dict):
            return "ERROR: args must be an object"
        if not task_id or not name:
            return "ERROR: task_id and name are required"
        draft = _plan_draft(ctx)
        try:
            draft.attach_postcondition(task_id, name, pc_args)
        except AgoraError as exc:
            return f"ERROR: {exc}"
        pc_count = len(draft.tasks[task_id]["postconditions"])
        return f"OK: postcondition {name!r} attached to {task_id!r} (task has {pc_count})"

    return plan_attach_postcondition


def _make_plan_add_llm_stage(ctx: ToolContext):
    async def plan_add_llm_stage(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        name = str(args.get("name", "")).strip()
        instruction = str(args.get("instruction", ""))
        context_files = args.get("context_files") or []
        max_iterations = int(args.get("max_iterations", 5))
        if not isinstance(context_files, list):
            return "ERROR: context_files must be a list"
        draft = _plan_draft(ctx)
        try:
            draft.add_llm_stage(
                task_id, name, instruction, context_files, max_iterations
            )
        except AgoraError as exc:
            return f"ERROR: {exc}"
        return f"OK: llm stage {name!r} added to {task_id!r}"

    return plan_add_llm_stage


def _make_plan_add_decision_stage(ctx: ToolContext):
    async def plan_add_decision_stage(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        name = str(args.get("name", "")).strip()
        decision_id = str(args.get("decision_id", "")).strip()
        question = str(args.get("question", ""))
        options = args.get("options") or []
        output_path = str(args.get("output_path", ""))
        if not isinstance(options, list):
            return "ERROR: options must be a list"
        draft = _plan_draft(ctx)
        try:
            draft.add_decision_stage(
                task_id, name, decision_id, question, options, output_path
            )
        except AgoraError as exc:
            return f"ERROR: {exc}"
        return (
            f"OK: decision stage {name!r} added to {task_id!r} "
            f"(decision_id={decision_id!r})"
        )

    return plan_add_decision_stage


def _make_plan_finalize(ctx: ToolContext):
    async def plan_finalize(args: dict[str, Any]) -> str:
        from pathlib import Path as _Path

        from agora.core.flow import save_flow
        from agora.plan.loader import instantiate_plan, load_plan

        draft = _plan_draft(ctx)

        # Apply optional late metadata edits.
        name = args.get("name")
        description = args.get("description")
        if name is not None or description is not None:
            try:
                draft.set_metadata(
                    str(name) if name else (draft.name or "planned"),
                    str(description) if description else draft.description,
                )
            except AgoraError as exc:
                return f"ERROR: {exc}"
        if not draft.name:
            draft.name = "planned"

        problems = draft.validate_ready()
        if problems:
            return "ERROR: plan not ready — " + "; ".join(problems)

        output_path = str(args.get("output_path", "plan/out.plan.yaml"))
        out_path = _safe_path(ctx.work_dir, output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            flow = draft.to_flow()
        except AgoraError as exc:
            return f"ERROR: failed to assemble Flow: {exc}"

        try:
            save_flow(flow, out_path)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: failed to write YAML: {exc}"

        # Round-trip: proves the emitted YAML loads + instantiates cleanly.
        try:
            reloaded = load_plan(out_path)
            project_name = draft.name or "_validate"
            r_agents, r_tasks, r_staged = instantiate_plan(
                reloaded, project_name=project_name
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"ERROR: emitted plan at {output_path!r} did not round-trip cleanly: {exc}"
            )

        # Record as an artifact so task-level postconditions (file_exists)
        # evaluate green and the write-event card posts.
        rel = output_path
        if rel not in ctx.written_files:
            ctx.written_files.append(rel)
        ctx.completions.append(
            {
                "summary": f"plan finalized: {len(r_tasks)} tasks, {len(r_staged)} staged",
                "artifacts": [rel],
            }
        )

        return (
            f"OK: wrote {rel} — {len(r_agents)} agents, {len(r_tasks)} tasks, "
            f"{len(r_staged)} staged. Run with `scripts/run_plan.py {rel}`."
        )

    return plan_finalize
