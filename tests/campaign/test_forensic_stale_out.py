"""Tests for scripts/forensic_stale_out.py — the stale-vs-live cell classifier."""

from __future__ import annotations

from scripts.forensic_stale_out import (
    classify_cell,
    classify_write_result,
    parse_writes_for_task,
)

_GUARD = ("t INFO x: tool call: task=loop_depth:concat turn=4 name=write_file "
          "args={'content': 'apple\\nboysenberry\\n', 'path': 'out/concat.txt'} "
          "result=ERROR: 'out/concat.txt' already exists with 59 bytes of content. "
          "write_file has been disabled")
_SUCCESS = ("t INFO x: tool call: task=small_chain:copy turn=2 name=write_file "
            "args={'content': 'hi', 'path': 'out/seed_copy.txt'} "
            "result=wrote 2 bytes to out/seed_copy.txt")


def test_classify_write_result() -> None:
    assert classify_write_result([]) is None  # never attempted
    assert classify_write_result(["wrote 2 bytes to out/x"]) == "success"
    assert classify_write_result(["ERROR: 'out/x' already exists with 5 bytes"]) == "guard_blocked"
    assert classify_write_result(["ERROR: permission denied"]) == "other_error"
    # success wins if any attempt succeeded
    assert classify_write_result(["ERROR: 'out/x' already exists with 5 bytes",
                                  "wrote 2 bytes to out/x"]) == "success"


def test_classify_cell_all_four_classes() -> None:
    assert classify_cell("success", True) == "live_pass"
    assert classify_cell("guard_blocked", True) == "stale_backed_pass"
    assert classify_cell(None, True) == "stale_backed_pass"
    assert classify_cell("guard_blocked", False) == "guard_artifact_fail"
    assert classify_cell("success", False) == "genuine_fail"
    assert classify_cell(None, False) == "genuine_fail"


def test_parse_writes_extracts_result_and_content() -> None:
    results, content = parse_writes_for_task(_GUARD, "loop_depth")
    assert classify_write_result(results) == "guard_blocked"
    assert content == "apple\nboysenberry\n"  # verbatim attempted bytes
    # a different task's writes are not attributed here
    results2, _ = parse_writes_for_task(_GUARD, "small_chain")
    assert results2 == []


def test_end_to_end_guard_artifact_fail() -> None:
    """guard-blocked write + failed equality ⇒ the failure is a stale-file artifact."""
    results, _ = parse_writes_for_task(_GUARD, "loop_depth")
    assert classify_cell(classify_write_result(results), predicate_passed=False) == "guard_artifact_fail"


def test_end_to_end_live_pass() -> None:
    results, content = parse_writes_for_task(_SUCCESS, "small_chain")
    assert content == "hi"
    assert classify_cell(classify_write_result(results), predicate_passed=True) == "live_pass"


def test_truncated_args_still_yield_result_and_content_prefix() -> None:
    """run.log truncates args at 120 chars — the guard-block result and the
    content prefix (incl. the marker) must survive an unterminated args dict."""
    truncated = ("t INFO x: tool call: task=small_chain:copy turn=2 name=write_file "
                 "args={'content': '[read_file#0] alpha line one\\nbeta line two', "
                 "'path': 'out/seed_copy.t result=ERROR: 'out/seed_copy.txt' already "
                 "exists with 66 bytes of content. write_file has been disabled")
    results, content = parse_writes_for_task(truncated, "small_chain")
    assert classify_write_result(results) == "guard_blocked"  # not lost to truncation
    assert content is not None and content.startswith("[read_file#0] ")  # marker visible
