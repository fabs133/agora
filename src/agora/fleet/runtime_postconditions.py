"""Postcondition factories that execute subprocesses to catch runtime bugs.

These sit alongside the static postcondition helpers in the project runner
scripts. Each factory returns a :class:`~agora.core.contract.Predicate` so it
plugs into ``Specification.postconditions`` unchanged.

The checks all expect the ctx dict to carry ``work_dir``. That matches the
contract the runner populates for every other postcondition.
"""

from __future__ import annotations

import re
from pathlib import Path

from agora.core.contract import make_predicate
from agora.fleet._subprocess import format_failure, run_host_python


def _require(name: str, check):
    return make_predicate(name, name, check)


def postcond_python_imports(rel: str, *, timeout: float = 15.0):
    """The module at ``rel`` must import without raising."""

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
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
        result = run_host_python(["-c", probe], cwd=work_dir, timeout=timeout)
        if result.ok:
            return (True, "")
        return (False, f"{rel} import failed: {format_failure(result, 1500)[:1500]}")

    return _require(f"{rel.replace('/', '_').replace('.', '_')}_imports"[:60], check)


def postcond_pytest_passes(rel: str = ".", *, timeout: float = 60.0):
    """``pytest rel`` must exit 0 inside work_dir."""

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing")
        if rel not in (".", "") and not (Path(work_dir) / rel).exists():
            return (False, f"{rel} does not exist under work_dir")
        result = run_host_python(
            [
                "-m", "pytest", rel,
                "-x", "--maxfail=1",
                "-q", "--tb=short", "--no-header",
                "-p", "no:cacheprovider",
            ],
            cwd=work_dir,
            timeout=timeout,
        )
        if result.ok:
            return (True, "")
        return (False, f"pytest failed for {rel}: {format_failure(result, 1800)[:1800]}")

    return _require(f"pytest_{rel.replace('/', '_').replace('.', '_')}"[:60], check)


def postcond_requirements_parse(rel: str = "requirements.txt"):
    """Every non-blank non-comment line of ``rel`` must be valid PEP 508."""

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        try:
            from packaging.requirements import InvalidRequirement, Requirement
        except ImportError:
            return (True, "")  # fail-open if packaging is not available
        errors: list[str] = []
        for i, raw in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            try:
                Requirement(line)
            except InvalidRequirement as exc:
                errors.append(f"line {i}: {raw!r}: {exc}")
        if errors:
            preview = "; ".join(errors[:3])
            return (False, f"{rel} has invalid lines: {preview}")
        return (True, "")

    return _require(f"{rel.replace('/', '_').replace('.', '_')}_parses"[:60], check)


def postcond_bot_calls_tree_sync(rel: str = "bot.py"):
    """``bot.py`` must reference ``tree.sync`` somewhere.

    This is a cheap string match rather than an AST probe: slash commands that
    are never sync'd to Discord never appear to the client, so the bot looks
    connected but does nothing. Catching it before the run ships saves a
    full end-to-end manual test.
    """

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        text = path.read_text(encoding="utf-8", errors="replace")
        if "tree.sync" in text:
            return (True, "")
        return (False, f"{rel} never calls tree.sync — slash commands will not register with Discord")

    return _require(f"{rel.replace('/', '_').replace('.', '_')}_calls_tree_sync"[:60], check)


def postcond_readme_only_references_existing_commands(
    readme: str = "README.md",
    bot: str = "bot.py",
):
    """Every ``/xxx`` command mentioned in the README must exist in ``bot.py``.

    Catches the LLM hallucinating features it didn't read about (the run-2
    README advertised a ``/help`` command nobody ever wrote).
    """

    _slash_re = re.compile(r"/([a-z][a-z0-9_]*)\b")
    _decl_re = re.compile(r"name\s*=\s*['\"]([a-z][a-z0-9_]*)['\"]")
    _ignore = {"usr", "bin", "env", "python", "python3"}

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing")
        readme_path = Path(work_dir) / readme
        bot_path = Path(work_dir) / bot
        if not readme_path.is_file():
            return (False, f"{readme} does not exist under work_dir")
        if not bot_path.is_file():
            return (False, f"{bot} does not exist under work_dir")
        readme_text = readme_path.read_text(encoding="utf-8", errors="replace")
        bot_text = bot_path.read_text(encoding="utf-8", errors="replace")
        mentioned = {m.group(1) for m in _slash_re.finditer(readme_text)} - _ignore
        declared = set(_decl_re.findall(bot_text))
        missing = sorted(mentioned - declared)
        if missing:
            return (
                False,
                f"{readme} references /{', /'.join(missing)} not declared in {bot}",
            )
        return (True, "")

    return _require("readme_commands_exist_in_bot", check)


def postcond_no_code_after_main_block(rel: str):
    """Catch module-scope statements placed after ``if __name__ == '__main__':``.

    Run 13 shipped a bug where ``build_roll``'s handler landed AFTER the
    ``bot.run(TOKEN)`` line — tests (which import with non-``__main__``
    ``__name__``) saw /roll registered; production (which hits the __main__
    block and blocks in ``bot.run``) never registered it.

    Detected via AST: find the module-scope ``if __name__ == '__main__':`` node
    and flag every subsequent top-level statement as unreachable at runtime.
    """
    from agora.fleet.inner_tools import _find_code_after_main_block

    def check(ctx):
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"could not read {rel}: {exc}")
        stragglers = _find_code_after_main_block(source)
        if not stragglers:
            return (True, "")
        preview = "; ".join(f"{kind}@L{lineno}" for kind, lineno in stragglers[:5])
        return (
            False,
            f"{rel} has module-scope code after `if __name__ == '__main__':` "
            f"(unreachable at runtime because bot.run blocks): {preview}",
        )

    return _require(
        f"{rel.replace('/', '_').replace('.', '_')}_no_code_after_main"[:60], check
    )


__all__ = [
    "postcond_bot_calls_tree_sync",
    "postcond_no_code_after_main_block",
    "postcond_pytest_passes",
    "postcond_python_imports",
    "postcond_readme_only_references_existing_commands",
    "postcond_requirements_parse",
]
