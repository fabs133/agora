"""Sphinx configuration for the Agora docs site.

Build locally:
    pip install -e ".[docs]"
    sphinx-build -b html docs docs/_build/html

Or with autobuild for live reload:
    sphinx-autobuild docs docs/_build/html
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the in-tree package importable for autodoc / autosummary.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# -- Project information -----------------------------------------------------

project = "Agora"
copyright = "2026, Agora contributors"
author = "Agora contributors"

# Version is derived from the package; keep in sync with pyproject.toml.
try:
    from agora import __version__ as release  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - sphinx safety net
    release = "0.1.0"
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
    "myst_parser",
    "sphinx_copybutton",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

master_doc = "index"

# Don't try to index build artefacts, the run-archive working files, or the
# README of the workspace dir (which is repo-tooling, not user docs).
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "runs/_inventory.csv",
    "runs/registry.yaml",
    "runs/registry_notes.yaml",
]

# -- HTML output -------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_title = f"Agora {version}"
html_static_path: list[str] = []
html_show_sourcelink = True

html_theme_options = {
    "navigation_depth": 3,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "titles_only": False,
}

# -- MyST (Markdown) ---------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "linkify",
    "replacements",
    "smartquotes",
    "strikethrough",
    "tasklist",
]
myst_heading_anchors = 3

# Avoid noisy warnings on the many cross-tree links from lessons-learned.md
# into ../src/, ../scripts/, ../workspace/ — those files exist on disk but
# aren't part of the Sphinx source tree by design.
suppress_warnings = ["myst.xref_missing", "myst.header"]

# -- Autodoc / autosummary ---------------------------------------------------

autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"
autodoc_class_signature = "separated"
autodoc_inherit_docstrings = True

# -- Napoleon (Google / NumPy docstrings) -----------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = False
napoleon_include_private_with_doc = False
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
}

# -- copybutton --------------------------------------------------------------

copybutton_prompt_text = r">>> |\.\.\. |\$ |#\s|In \[\d*\]: "
copybutton_prompt_is_regexp = True

# -- todo --------------------------------------------------------------------

todo_include_todos = False

# -- Build behaviour ---------------------------------------------------------

nitpicky = False  # cross-tree links into src/ would noise this up
