"""Tests for scripts/autopsy_final_turns.py classifiers + log parsing."""

from __future__ import annotations

from scripts.autopsy_final_turns import (
    classify_final_turn,
    classify_mark_complete,
    parse_run_log,
)


def test_classify_mark_complete() -> None:
    assert classify_mark_complete({"summary": "x", "artifacts": ["a"]}) == "summary_ok"
    assert classify_mark_complete({"path": "out/x.txt", "content": "y"}) == "write_file_args"
    assert classify_mark_complete({"foo": 1}) == "other_malformed"


def test_classify_final_turn() -> None:
    assert classify_final_turn(0, 0, False) == "empty"
    assert classify_final_turn(0, 42, False) == "prose_no_call"
    assert classify_final_turn(1, 0, True) == "malformed_call"
    assert classify_final_turn(1, 0, False) == "max_iter"


def test_parse_run_log_captures_final_turn_and_malformed_mark_complete() -> None:
    # Mirrors the r019 pattern: mark_complete called with write_file args, which
    # errors; the final turn for the task is empty.
    log = "\n".join([
        "t INFO x: llm return: task=small_chain:copy turn=1 tool_calls=1 content_len=0",
        "t INFO x: tool call: task=small_chain:copy turn=2 name=mark_complete "
        "args={'path': 'out/seed_copy.txt', 'content': 'alpha\\nbeta'} "
        "result=ERROR: tool mark_complete raised: 'summary'",
        "t INFO x: llm return: task=small_chain:copy turn=3 tool_calls=0 content_len=0",
        "t INFO x: task small_chain done: success=False iterations=3",
    ])
    finals, errored, mc = parse_run_log(log)
    assert finals["small_chain"] == (3, 0, 0)  # last turn wins; empty
    assert ("small_chain", 2) in errored
    assert mc == ["write_file_args"]
    # end-to-end: the final-turn class for this failed cell is 'empty'
    turn, calls, clen = finals["small_chain"]
    assert classify_final_turn(calls, clen, ("small_chain", turn) in errored) == "empty"
