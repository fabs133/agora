#!/usr/bin/env python
"""Merge gates for the config-hardening invariants.

Run it: ``python scripts/check_hardening_invariants.py`` (no deps, stdlib only).
Non-zero exit on any violation. CI runs this; run it yourself before a PR.

These three rules are what "one config source, injected sinks" MEANS in
practice. They were established by hand-auditing 58 env reads and 27 localhost
literals across 15 files, and nothing but a gate keeps them true: the survey
that found them was a one-off, and the next `os.getenv` is one convenient
moment away. Encoded here so the invariant outlives the person who remembers it.

  (a) env is read in ONE place. `os.getenv("AGORA_...")` outside config.py is
      forbidden, except a small registered allowlist of debug flags — each one
      an explicit, named exception rather than an accident.
  (b) no hardcoded localhost. Endpoints are config-shaped and injected; a
      localhost default buried in a sink is how a "configurable" endpoint
      silently isn't. Only config.py's defaults and .env.example may carry one.
  (c) Settings is imported only at a composition root. A library module that
      reads Settings has hidden global state and can't be tested or reused with
      different config; entrypoints resolve config and inject it downward.

Adding an exception is a deliberate act: put it in the allowlist below, with a
reason. That is the point — the cost of the exception is that you must name it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- (a) env reads

#: The ONLY AGORA_* env vars readable outside config.py. Four debug flags that
#: exist to be flipped ad hoc without a config round-trip, plus the PlantUML
#: dev-tool endpoint (render_diagrams.py is standalone: it must run in a bare
#: checkout with no agora install, so it cannot import Settings).
ENV_ALLOWLIST = {
    "AGORA_STRUCTURE_STRICT",       # debug: strict structure checks
    "AGORA_SERIAL_TASKS",           # debug: force serial dispatch
    "AGORA_DECISION_TIMEOUT_SECONDS",  # debug: shorten decision waits
    "AGORA_RUN_OUTPUT_DIR",         # debug: redirect provenance output
    "AGORA_PLANTUML_URL",           # dev tooling, stdlib-only script
}

_ENV_CALL = re.compile(
    r"""os\.(?:getenv|environ\.get)\(\s*["'](AGORA_[A-Z0-9_]*)["']|os\.environ\[\s*["'](AGORA_[A-Z0-9_]*)["']"""
)

# ---------------------------------------------------------------- (b) localhost

#: A *string literal* pointing at localhost. Prose mentioning the word
#: "localhost" (e.g. "no localhost default here") is fine and common — the rule
#: is about literals that become behaviour, not about the word.
_LOCALHOST_LITERAL = re.compile(r"""["']\w*://localhost""")

LOCALHOST_ALLOWED = {
    "src/agora/config.py",              # the defaults live here, by design
    "scripts/render_diagrams.py",       # stdlib-only dev tool, AGORA_PLANTUML_URL-overridable
}

# ---------------------------------------------------------------- (c) Settings imports

#: Composition roots: the places allowed to resolve config and inject it down.
SETTINGS_IMPORT_ALLOWED_PREFIXES = (
    "src/agora/config.py",
    "src/agora/cli.py",
    "scripts/",
    "tests/",
)


def _py_files() -> list[Path]:
    out: list[Path] = []
    for base in ("src", "scripts"):
        out += [
            p for p in (REPO / base).rglob("*.py")
            if "__pycache__" not in p.parts and ".venv" not in p.parts
        ]
    return sorted(out)


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def _strip_strings_and_comments(src: str) -> str:
    """Blank out comments so prose can't trip the literal checks.

    Docstrings are left alone deliberately for (b): a docstring showing
    ``AGORA_OLLAMA_BASE_URL=http://localhost:11434`` as an example is
    documentation, and the regex only matches QUOTED literals anyway.
    """
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def check_env_reads(files: list[Path]) -> list[str]:
    bad: list[str] = []
    for p in files:
        rel = _rel(p)
        if rel == "src/agora/config.py":
            continue
        for i, line in enumerate(_strip_strings_and_comments(p.read_text(encoding="utf-8")).splitlines(), 1):
            for m in _ENV_CALL.finditer(line):
                var = m.group(1) or m.group(2)
                if var not in ENV_ALLOWLIST:
                    bad.append(
                        f"{rel}:{i}: reads {var} outside config.py. Env is read in ONE place — "
                        f"add it to Settings and inject it, or register it in ENV_ALLOWLIST with a reason."
                    )
    return bad


def check_localhost_literals(files: list[Path]) -> list[str]:
    bad: list[str] = []
    for p in files:
        rel = _rel(p)
        if rel in LOCALHOST_ALLOWED:
            continue
        for i, line in enumerate(_strip_strings_and_comments(p.read_text(encoding="utf-8")).splitlines(), 1):
            if _LOCALHOST_LITERAL.search(line):
                bad.append(
                    f"{rel}:{i}: hardcoded localhost URL. Endpoints are config-shaped and injected "
                    f"from Settings — a localhost default in a sink makes a 'configurable' endpoint silently fixed."
                )
    return bad


def check_settings_imports(files: list[Path]) -> list[str]:
    bad: list[str] = []
    for p in files:
        rel = _rel(p)
        if rel.startswith(SETTINGS_IMPORT_ALLOWED_PREFIXES):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = next((a.name for a in node.names if a.name.startswith("agora.config")), None)
            if mod and (mod == "agora.config" or mod.startswith("agora.config.")):
                bad.append(
                    f"{rel}:{node.lineno}: imports agora.config outside a composition root. "
                    f"A library module that reads Settings carries hidden global state — "
                    f"take config-shaped params and let an entrypoint inject them."
                )
    return bad


def main() -> int:
    files = _py_files()
    checks = (
        ("(a) env read in one place", check_env_reads(files)),
        ("(b) no hardcoded localhost", check_localhost_literals(files)),
        ("(c) Settings only at a composition root", check_settings_imports(files)),
    )
    failed = 0
    for name, violations in checks:
        if violations:
            failed += 1
            print(f"FAIL {name}: {len(violations)} violation(s)")
            for v in violations:
                print(f"  {v}")
        else:
            print(f"ok   {name}")
    if failed:
        print(
            f"\n{failed} hardening invariant(s) violated. These are MERGE GATES: the "
            f"'one config source, injected sinks' design is only real while they hold."
        )
        return 1
    print(f"\nall hardening invariants hold ({len(files)} files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
