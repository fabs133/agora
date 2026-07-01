"""Unit tests for the three axis-1 probe predicates (happy + fail-closed + mismatch)."""

from __future__ import annotations

from agora.plan.predicate_registry import build_predicate


def _ctx(tmp_path, **extra):
    base = {"work_dir": str(tmp_path), "artifacts": [], "completions": []}
    base.update(extra)
    return base


def _write(tmp_path, rel, data: bytes) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


# --------------------------------------------------------------- equals_seed


def test_equals_seed_happy(tmp_path) -> None:
    _write(tmp_path, "plan/seed.txt", b"line one\nline two\n")
    _write(tmp_path, "out/copy.txt", b"line one\nline two\n")
    pred = build_predicate(
        "file_content_equals_seed", {"path": "out/copy.txt", "seed_path": "plan/seed.txt"}
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is True


def test_equals_seed_missing_output_fails_closed(tmp_path) -> None:
    _write(tmp_path, "plan/seed.txt", b"x")
    pred = build_predicate(
        "file_content_equals_seed", {"path": "out/copy.txt", "seed_path": "plan/seed.txt"}
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is False


def test_equals_seed_missing_seed_fails_closed(tmp_path) -> None:
    _write(tmp_path, "out/copy.txt", b"x")
    pred = build_predicate(
        "file_content_equals_seed", {"path": "out/copy.txt", "seed_path": "plan/seed.txt"}
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is False


def test_equals_seed_mismatch(tmp_path) -> None:
    _write(tmp_path, "plan/seed.txt", b"alpha\n")
    _write(tmp_path, "out/copy.txt", b"alpha\n  ")  # trailing whitespace differs
    pred = build_predicate(
        "file_content_equals_seed", {"path": "out/copy.txt", "seed_path": "plan/seed.txt"}
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is False


# --------------------------------------------------------------- equals_concat


def test_equals_concat_happy_no_separator(tmp_path) -> None:
    _write(tmp_path, "plan/a.txt", b"AAA")
    _write(tmp_path, "plan/b.txt", b"BBB")
    _write(tmp_path, "out/c.txt", b"AAABBB")
    pred = build_predicate(
        "file_content_equals_concat",
        {"path": "out/c.txt", "first_path": "plan/a.txt", "second_path": "plan/b.txt"},
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is True


def test_equals_concat_missing_fails_closed(tmp_path) -> None:
    _write(tmp_path, "plan/a.txt", b"AAA")
    _write(tmp_path, "out/c.txt", b"AAABBB")
    pred = build_predicate(
        "file_content_equals_concat",
        {"path": "out/c.txt", "first_path": "plan/a.txt", "second_path": "plan/b.txt"},
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is False  # b.txt missing


def test_equals_concat_wrong_order_or_separator(tmp_path) -> None:
    _write(tmp_path, "plan/a.txt", b"AAA")
    _write(tmp_path, "plan/b.txt", b"BBB")
    _write(tmp_path, "out/c.txt", b"AAA\nBBB")  # spurious separator
    pred = build_predicate(
        "file_content_equals_concat",
        {"path": "out/c.txt", "first_path": "plan/a.txt", "second_path": "plan/b.txt"},
    )
    assert pred.evaluate(_ctx(tmp_path))[0] is False


# --------------------------------------------------------------- mark_complete_called


def test_mark_complete_called_happy() -> None:
    pred = build_predicate("mark_complete_called", {})
    assert pred.evaluate({"completions": [{"summary": "done"}]})[0] is True


def test_mark_complete_called_never(tmp_path) -> None:
    pred = build_predicate("mark_complete_called", {})
    assert pred.evaluate({"completions": []})[0] is False
    assert pred.evaluate({})[0] is False
