"""Automatic validation + git + completion hooks fired after tool calls.

Weak LLMs (qwen2.5:7b and similar) cannot reliably juggle a multi-step protocol
of ``write_file`` → ``check_python`` → ``run_python_import`` → ``git_commit`` →
``mark_complete``. They burn iterations on orchestration and bail before calling
``mark_complete``. This module runs those validation + bookkeeping tools
*automatically* on the framework side, so the agent only has to decide what to
write.

The hooks are passive from the LLM's perspective: the results of auto-run tools
are appended to the message history as synthetic ``tool_result`` entries, so the
model sees ``check_python`` errors on its next turn and can fix the file — but
it never has to remember to call the tool itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agora.fleet.inner_tools import (
    ToolContext,
    _make_check_python,
    _make_check_requirements,
    _make_git_commit,
    _make_run_python_import,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoHookResult:
    """One automatically-run tool's output."""

    tool_name: str
    arguments: dict[str, Any]
    result: str
    success: bool


async def run_auto_hooks(
    call_name: str,
    call_arguments: dict[str, Any],
    call_result: str,
    ctx: ToolContext,
) -> list[AutoHookResult]:
    """Return the list of auto-invocations triggered by a finished tool call.

    Currently only ``write_file`` triggers hooks. The chain is:

    - on ``.py`` files: ``check_python`` → (if clean) ``run_python_import``
    - on ``requirements.txt``: ``check_requirements``
    - after any successful write with a git repo: ``git_commit``

    A hook chain stops on the first failure so the agent sees one focused
    error at a time.
    """
    if not ctx.auto_hooks_enabled:
        return []

    # Resolve which path (if any) was written by this tool call. Four tools
    # land bytes on disk:
    #   - ``write_file`` (always)
    #   - ``fetch_url`` when ``save_as`` is set
    #   - ``edit_file_replace`` / ``edit_file_insert_before`` / ``edit_file_append``
    # All of these take ``path`` as an argument and return a result string that
    # does NOT start with ``ERROR`` on success.
    rel: str = ""
    if not isinstance(call_result, str):
        return []
    if call_result.startswith("ERROR"):
        return []

    if call_name == "write_file":
        if not call_result.startswith("wrote "):
            return []
        rel = str(call_arguments.get("path", "") or "")
    elif call_name == "fetch_url":
        save_as = call_arguments.get("save_as")
        if not save_as:
            return []
        if not call_result.startswith("fetched "):
            return []
        rel = str(save_as)
    elif call_name in ("edit_file_replace", "edit_file_insert_before", "edit_file_append"):
        rel = str(call_arguments.get("path", "") or "")
    else:
        return []

    if not rel:
        return []

    hooks: list[AutoHookResult] = []

    lower = rel.lower()
    python_ok = True
    if lower.endswith(".py"):
        cp_result = await _make_check_python(ctx)({"path": rel})
        cp_success = cp_result.startswith("OK")
        hooks.append(
            AutoHookResult(
                tool_name="check_python",
                arguments={"path": rel},
                result=cp_result,
                success=cp_success,
            )
        )
        python_ok = cp_success
        if cp_success:
            ri_result = await _make_run_python_import(ctx)({"path": rel})
            ri_success = ri_result.startswith("OK")
            hooks.append(
                AutoHookResult(
                    tool_name="run_python_import",
                    arguments={"path": rel},
                    result=ri_result,
                    success=ri_success,
                )
            )
            python_ok = ri_success
    elif Path(lower).name == "requirements.txt":
        cr_result = await _make_check_requirements(ctx)({"path": rel})
        cr_success = cr_result.startswith("OK")
        hooks.append(
            AutoHookResult(
                tool_name="check_requirements",
                arguments={"path": rel},
                result=cr_result,
                success=cr_success,
            )
        )

    # Only commit if validation passed (or there was no python-level check).
    validation_clean = python_ok and all(h.success for h in hooks)
    if validation_clean and ctx.git_repo is not None:
        commit_message = f"auto: wrote {rel}"
        try:
            commit_result = await _make_git_commit(ctx)({"message": commit_message})
            success = not commit_result.startswith("ERROR")
        except Exception as exc:  # noqa: BLE001
            # A "nothing to commit" edge (edit produced identical content, or
            # the file is already staged/committed) shouldn't fail the whole
            # task. Record it as a non-fatal hook result and move on.
            commit_result = f"ERROR: git_commit raised: {exc}"
            success = False
        hooks.append(
            AutoHookResult(
                tool_name="git_commit",
                arguments={"message": commit_message},
                result=commit_result,
                success=success,
            )
        )

    for h in hooks:
        logger.info(
            "auto-hook: %s on %s -> %s",
            h.tool_name,
            rel,
            "OK" if h.success else "FAIL",
        )
    return hooks


def synthesize_mark_complete(ctx: ToolContext, final_text: str) -> bool:
    """Append a synthetic ``mark_complete`` entry if the LLM never called one.

    Returns True when a synthetic entry was appended, False when the LLM's own
    ``mark_complete`` was present (nothing to do).

    The summary is derived from ``final_text`` (last LLM content) and the
    artifact list is taken from ``ctx.written_files``. This keeps the
    ``_postcond_mark_complete`` contract satisfied even when the model bails
    at ``tool_calls=0`` without the courtesy of calling the tool itself —
    which is the dominant failure mode on weak models.
    """
    if ctx.completions:
        return False
    summary = _derive_summary(final_text)
    artifacts = list(ctx.written_files)
    ctx.completions.append({"summary": summary, "artifacts": artifacts, "auto": True})
    logger.info(
        "auto-mark-complete synthesized: summary=%r artifacts=%d",
        summary,
        len(artifacts),
    )
    return True


def _derive_summary(final_text: str) -> str:
    text = (final_text or "").strip()
    if not text:
        return "(auto-synthesized completion; agent produced no final text)"
    first_line = text.splitlines()[0].strip()
    return first_line[:200] if first_line else text[:200]


__all__ = [
    "AutoHookResult",
    "run_auto_hooks",
    "synthesize_mark_complete",
]
