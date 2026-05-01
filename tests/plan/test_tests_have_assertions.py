"""Sprint 7.1: tests_have_assertions predicate.

Forces the tester's LLM stage to replace every ``pytest.skip`` stub with real
assertions. The scaffolder emits ``def test_x(): pytest.skip("TODO: ...")``
per deliverable; without this gate, pytest passes trivially with zero signal.
"""

from __future__ import annotations

from pathlib import Path

from agora.plan.predicate_registry import (
    build_predicate,
    postcond_tests_have_assertions,
)


def _eval(work_dir: Path, rel: str = "tests/test_x.py") -> tuple[bool, str]:
    p = postcond_tests_have_assertions(rel)
    return p.evaluate({"work_dir": str(work_dir)})


def test_name_matches_registry_pattern():
    p = postcond_tests_have_assertions("tests/test_contract.py")
    assert p.name == "tests_test_contract_py_has_assertions"


def test_passes_on_real_assertions(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "\n"
        "def test_add_returns_six_chars():\n"
        "    \"\"\"Add returns a 6-char hash.\"\"\"\n"
        "    from mypkg import add\n"
        "    result = add('https://example.com')\n"
        "    assert len(result) == 6\n",
        encoding="utf-8",
    )
    p = postcond_tests_have_assertions("tests/test_x.py")
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is True, reason


def test_fails_when_all_bodies_are_pytest_skip(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "\n"
        "def test_one():\n"
        "    \"\"\"TODO first.\"\"\"\n"
        "    pytest.skip('TODO: fill in')\n"
        "\n"
        "def test_two():\n"
        "    pytest.skip('TODO: other')\n",
        encoding="utf-8",
    )
    p = postcond_tests_have_assertions("tests/test_x.py")
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "2/2" in reason and "placeholders" in reason
    assert "test_one" in reason
    assert "test_two" in reason


def test_fails_when_one_body_is_still_skip(tmp_path: Path):
    """Partial fills count as failure — EVERY stub must be replaced."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "\n"
        "def test_real():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "def test_stub():\n"
        "    pytest.skip('TODO')\n",
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is False
    assert "1/2" in reason
    assert "test_stub" in reason
    assert "test_real" not in reason


def test_fails_when_no_test_functions_at_all(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        '"""Empty test file."""\nimport pytest\n',
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is False
    assert "no test_* functions" in reason


def test_fails_on_missing_file(tmp_path: Path):
    passed, reason = _eval(tmp_path)
    assert passed is False
    assert "does not exist" in reason


def test_fails_on_syntax_error(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_x(\n", encoding="utf-8")
    passed, reason = _eval(tmp_path)
    assert passed is False
    assert "SyntaxError" in reason


def test_requires_work_dir():
    p = postcond_tests_have_assertions("tests/anything.py")
    passed, reason = p.evaluate({})
    assert passed is False
    assert "work_dir missing" in reason


def test_docstring_plus_skip_still_counts_as_placeholder(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "\n"
        "def test_x():\n"
        "    \"\"\"Docstring.\"\"\"\n"
        "    pytest.skip('TODO')\n",
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is False


def test_class_based_tests_counted(tmp_path: Path):
    """TestX-prefixed class method test_* is inspected like top-level ones."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "\n"
        "class TestThing:\n"
        "    def test_real(self):\n"
        "        assert True\n"
        "\n"
        "    def test_stub(self):\n"
        "        pytest.skip('TODO')\n",
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is False
    assert "test_stub" in reason


def test_pass_only_body_is_placeholder(tmp_path: Path):
    """A test with nothing but ``pass`` has no real assertion — placeholder."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "def test_x():\n    pass\n",
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is False


def test_deferred_import_plus_assert_passes(tmp_path: Path):
    """Contract-style deferred imports inside the body count as real content."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "def test_x():\n"
        "    from mypkg import do_thing\n"
        "    assert do_thing('x') is not None\n",
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is True, reason


def test_xfail_body_still_placeholder(tmp_path: Path):
    """``pytest.xfail(...)`` as the only body is also a placeholder."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n\ndef test_x():\n    pytest.xfail('later')\n",
        encoding="utf-8",
    )
    passed, reason = _eval(tmp_path)
    assert passed is False


def test_build_predicate_via_registry(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "def test_x():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    p = build_predicate("tests_have_assertions", {"rel": "tests/test_x.py"})
    passed, _ = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is True
