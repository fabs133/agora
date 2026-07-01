"""Probe-specific postconditions for the tool-call fidelity probe (axis 1).

These three predicates back ``flows/tool-call-fidelity.plan.yaml``. They are
deliberately narrow and byte-exact so a failure attributes to the tool-call
axis (did the model read/concatenate/copy bytes correctly and call the tools
in order) rather than to code-generation or judgment.

Registered into :mod:`agora.plan.predicate_registry` via the standard
``@register_predicate`` decorator; importing this module performs the
registration (the registry imports it at the bottom of its own module so the
names resolve everywhere, exactly like the built-in factories).

All file-reading predicates **fail closed**: a missing target file returns
``False``, never a vacuous pass (see the Checkpoint-1 fail-closed audit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agora.core.contract import Predicate, make_predicate
from agora.plan.predicate_registry import register_predicate


def _require(name: str, check) -> Predicate:
    return make_predicate(name, name, check)


def _read_bytes(work_dir: str, rel: str) -> bytes | None:
    """Read ``rel`` under ``work_dir`` as bytes; None if missing/unreadable."""
    path = Path(work_dir) / rel
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


@register_predicate("file_content_equals_seed")
def postcond_file_content_equals_seed(path: str, seed_path: str) -> Predicate:
    """``path`` must be byte-exact equal to ``seed_path`` (both under work_dir).

    Fails closed: if either file is missing/unreadable, returns False.
    """

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        got = _read_bytes(work_dir, path)
        if got is None:
            return (False, f"{path} does not exist under work_dir")
        want = _read_bytes(work_dir, seed_path)
        if want is None:
            return (False, f"seed {seed_path} does not exist under work_dir")
        if got == want:
            return (True, "")
        return (
            False,
            f"{path} ({len(got)} bytes) does not byte-match {seed_path} "
            f"({len(want)} bytes)",
        )

    name = f"{path}_eq_seed_{seed_path}".replace("/", "_").replace(".", "_")
    return _require(name[:60], check)


@register_predicate("file_content_equals_concat")
def postcond_file_content_equals_concat(
    path: str, first_path: str, second_path: str
) -> Predicate:
    """``path`` must equal ``first_path`` bytes followed by ``second_path`` bytes
    (no separator). All three are work_dir-relative.

    Fails closed: any missing file returns False.
    """

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        got = _read_bytes(work_dir, path)
        if got is None:
            return (False, f"{path} does not exist under work_dir")
        a = _read_bytes(work_dir, first_path)
        if a is None:
            return (False, f"{first_path} does not exist under work_dir")
        b = _read_bytes(work_dir, second_path)
        if b is None:
            return (False, f"{second_path} does not exist under work_dir")
        if got == a + b:
            return (True, "")
        return (
            False,
            f"{path} ({len(got)} bytes) != concat({first_path}+{second_path}) "
            f"({len(a) + len(b)} bytes)",
        )

    name = (
        f"{path}_eq_concat_{first_path}_{second_path}".replace("/", "_").replace(".", "_")
    )
    return _require(name[:60], check)


@register_predicate("mark_complete_called")
def postcond_mark_complete_called() -> Predicate:
    """The task must have recorded a completion (``ctx.completions`` non-empty).

    Mirrors the built-in ``mark_complete`` registry predicate; exposed under
    the ``mark_complete_called`` name the probe YAML references directly.
    """

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        return (bool(ctx.get("completions")), "mark_complete was not called")

    return _require("mark_complete_called", check)


__all__ = [
    "postcond_file_content_equals_concat",
    "postcond_file_content_equals_seed",
    "postcond_mark_complete_called",
]
