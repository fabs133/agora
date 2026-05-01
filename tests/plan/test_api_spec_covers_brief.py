"""Approach C (C1): api_spec_covers_brief_deliverables predicate.

Gates ``define_api`` — every brief ``## Key deliverables`` bullet must map
to a method or function in ``plan/api_spec.md`` via the verb→keyword
dictionary.
"""

from __future__ import annotations

from pathlib import Path

from agora.plan.predicate_registry import (
    _BRIEF_VERB_KEYWORDS,
    _extract_brief_bullets,
    _extract_verbs,
    build_predicate,
    postcond_api_spec_covers_brief_deliverables,
)

_URL_SHORTENER_BRIEF = """\
# Brief: URL Shortener

Build a URL shortener CLI.

## Key deliverables
- User can add a long URL and get a short 6-char hash back
- can look up the original URL given the hash
- can list all saved mappings
- Persist to disk
- Include tests for add/lookup/list plus collision handling
"""

_COMPLETE_SPEC = """\
# API spec

## module: src/url_shortener.py

class URLShortener:
    def __init__(self) -> None: ...
    def add_url(self, long_url: str) -> str: ...
    def lookup_hash(self, h: str) -> str: ...
    def list_mappings(self) -> list: ...
    def save(self, path: str) -> None: ...
    def load(self, path: str) -> None: ...
"""

_MISSING_PERSIST_SPEC = """\
# API spec

## module: src/url_shortener.py

class URLShortener:
    def __init__(self) -> None: ...
    def add_url(self, long_url: str) -> str: ...
    def lookup_hash(self, h: str) -> str: ...
    def list_mappings(self) -> list: ...
"""


def _eval(tmp_path: Path, brief: str, spec: str) -> tuple[bool, str]:
    (tmp_path / "plan").mkdir(exist_ok=True)
    (tmp_path / "plan" / "brief.md").write_text(brief, encoding="utf-8")
    (tmp_path / "plan" / "api_spec.md").write_text(spec, encoding="utf-8")
    pred = postcond_api_spec_covers_brief_deliverables()
    return pred.evaluate({"work_dir": str(tmp_path)})


# --------------------------------------------------------------- happy paths


def test_complete_spec_passes(tmp_path: Path):
    passed, reason = _eval(tmp_path, _URL_SHORTENER_BRIEF, _COMPLETE_SPEC)
    assert passed is True, reason


def test_empty_deliverables_section_passes(tmp_path: Path):
    """Brief without a ``## Key deliverables`` section = nothing to enforce."""
    brief = "# Brief\n\nSome prose with no structured deliverables.\n"
    spec = "## module: src/x.py\n\ndef foo() -> None: ...\n"
    passed, _ = _eval(tmp_path, brief, spec)
    assert passed is True


def test_bullet_without_actionable_verb_passes(tmp_path: Path):
    """Prose-only bullet (no verb the dictionary recognises) passes vacuously."""
    brief = (
        "# Brief\n\n"
        "## Key deliverables\n"
        "- Something elegant and well-designed\n"
    )
    spec = "## module: src/x.py\n\ndef foo() -> None: ...\n"
    passed, _ = _eval(tmp_path, brief, spec)
    assert passed is True


def test_functions_only_spec_covers_bullets(tmp_path: Path):
    """Top-level functions (no class) are valid coverage."""
    brief = (
        "# Brief\n\n"
        "## Key deliverables\n"
        "- Add a record to the database\n"
        "- Retrieve records by id\n"
    )
    spec = (
        "## module: src/db.py\n\n"
        "def add_record(rec: dict) -> int: ...\n"
        "def get_record(rid: int) -> dict: ...\n"
    )
    passed, reason = _eval(tmp_path, brief, spec)
    assert passed is True, reason


# --------------------------------------------------------------- failure paths


def test_missing_persist_fails(tmp_path: Path):
    passed, reason = _eval(tmp_path, _URL_SHORTENER_BRIEF, _MISSING_PERSIST_SPEC)
    assert passed is False
    # Error message must name the offending bullet.
    assert "persist" in reason.lower() or "disk" in reason.lower()
    # Error message must include the fix hint.
    assert "fix:" in reason.lower()


def test_missing_bullet_error_lists_known_symbols(tmp_path: Path):
    """On failure, the message exposes the methods the architect DID write
    so the retry can reason about what's there already."""
    passed, reason = _eval(tmp_path, _URL_SHORTENER_BRIEF, _MISSING_PERSIST_SPEC)
    assert passed is False
    # add_url IS in the spec, so it shows up in 'known api_spec symbols'.
    assert "add_url" in reason


def test_completely_empty_spec_fails(tmp_path: Path):
    """A spec with a module header but no methods → every verb-bearing
    bullet fails because there's nothing to match."""
    brief = (
        "# Brief\n\n"
        "## Key deliverables\n"
        "- Add a new record\n"
    )
    spec = "## module: src/x.py\n\n"
    passed, reason = _eval(tmp_path, brief, spec)
    assert passed is False


# --------------------------------------------------------------- missing files


def test_missing_spec_file_fails(tmp_path: Path):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "brief.md").write_text(
        _URL_SHORTENER_BRIEF, encoding="utf-8"
    )
    pred = postcond_api_spec_covers_brief_deliverables()
    passed, reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_missing_brief_file_fails(tmp_path: Path):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "api_spec.md").write_text(_COMPLETE_SPEC, encoding="utf-8")
    pred = postcond_api_spec_covers_brief_deliverables()
    passed, reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_missing_work_dir_fails(tmp_path: Path):
    pred = postcond_api_spec_covers_brief_deliverables()
    passed, reason = pred.evaluate({})
    assert passed is False
    assert "work_dir" in reason


# --------------------------------------------------------------- via registry


def test_via_registry_round_trips(tmp_path: Path):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "brief.md").write_text(
        _URL_SHORTENER_BRIEF, encoding="utf-8"
    )
    (tmp_path / "plan" / "api_spec.md").write_text(
        _COMPLETE_SPEC, encoding="utf-8"
    )
    pred = build_predicate(
        "api_spec_covers_brief_deliverables",
        {"rel": "plan/api_spec.md", "brief_rel": "plan/brief.md"},
    )
    passed, reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is True, reason


def test_predicate_name_is_stable():
    """Predicate name is used in fingerprints and telemetry — must not drift."""
    pred = postcond_api_spec_covers_brief_deliverables()
    # Matches the _require() naming convention from this module.
    assert "covers_brief" in pred.name


# --------------------------------------------------------------- verb extraction


def test_extract_verbs_finds_dictionary_tokens():
    bullet = "User can add a long URL and look up mappings from disk"
    verbs = _extract_verbs(bullet)
    # Expect at least 'add', 'look', 'disk' (or 'lookup'/'find' variants)
    assert "add" in verbs
    # Either 'look' or 'lookup' depending on how the brief is phrased
    assert any(v in verbs for v in ("look", "lookup", "find"))
    assert "disk" in verbs


def test_extract_verbs_empty_on_prose_only():
    assert _extract_verbs("Something elegant") == []
    assert _extract_verbs("") == []


def test_extract_verbs_deduplicates():
    # Three 'add' tokens → one entry. 'new' and 'record' are not dictionary
    # keys (they're only values for 'add' / 'create').
    assert _extract_verbs("add add add record") == ["add"]


def test_extract_verbs_mixed_bullet_returns_multiple_verbs():
    # 'add' and 'list' are both keys → both returned, in appearance order.
    assert _extract_verbs("add a URL and list mappings") == ["add", "list"]


def test_extract_brief_bullets_finds_deliverables_section():
    brief = (
        "# Brief\n\n"
        "Some intro\n\n"
        "## Key deliverables\n"
        "- First bullet\n"
        "- Second bullet\n"
        "* Third bullet (asterisk)\n"
        "\n"
        "## Other section\n"
        "- Should not appear\n"
    )
    bullets = _extract_brief_bullets(brief)
    assert bullets == ["First bullet", "Second bullet", "Third bullet (asterisk)"]


def test_extract_brief_bullets_case_insensitive_heading():
    brief = "## KEY DELIVERABLES\n- A\n- B\n"
    assert _extract_brief_bullets(brief) == ["A", "B"]


def test_extract_brief_bullets_empty_without_section():
    assert _extract_brief_bullets("Just prose") == []
    assert _extract_brief_bullets("") == []


# --------------------------------------------------------------- verb dictionary


def test_verb_dictionary_covers_common_verbs():
    # Smoke test that the common CRUD verbs from observed briefs are present.
    for verb in ("add", "lookup", "list", "persist", "save", "load", "delete"):
        assert verb in _BRIEF_VERB_KEYWORDS, f"{verb} missing from dictionary"
