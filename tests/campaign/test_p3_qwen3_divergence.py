"""Tests for scripts/p3_qwen3_divergence.py — turn signatures + first-divergence."""

from __future__ import annotations

from scripts.p3_qwen3_divergence import first_divergence, turn_signatures

_LOG = "\n".join([
    "t INFO x: llm return: task=small_chain:copy turn=1 tool_calls=2 content_len=0",
    "t INFO x: llm return: task=small_chain:copy turn=2 tool_calls=1 content_len=0",
    "t INFO x: llm return: task=loop_depth:concat turn=1 tool_calls=1 content_len=0",
    "t INFO x: llm return: task=small_chain:copy turn=3 tool_calls=0 content_len=46",
])


def test_turn_signatures_filters_by_task() -> None:
    assert turn_signatures(_LOG, "small_chain") == [(1, 2, 0), (2, 1, 0), (3, 0, 46)]
    assert turn_signatures(_LOG, "loop_depth") == [(1, 1, 0)]


def test_first_divergence_at_turn_zero() -> None:
    # call-vs-no-call at turn 1 (the qwen3 fail/0 vs pass split)
    fail0 = [(1, 0, 0)]
    pass3 = [(1, 2, 0), (2, 1, 0), (3, 0, 46)]
    assert first_divergence([fail0, pass3]) == 0


def test_first_divergence_later_when_turn_one_shared() -> None:
    # pass/3 vs pass/4 share t1,t2 and first differ at turn index 2 (turn 3)
    pass3 = [(1, 2, 0), (2, 1, 0), (3, 0, 46)]
    pass4 = [(1, 2, 0), (2, 1, 0), (3, 1, 0), (4, 0, 0)]
    assert first_divergence([pass3, pass4]) == 2


def test_first_divergence_none_when_identical() -> None:
    seq = [(1, 2, 0), (2, 1, 0)]
    assert first_divergence([seq, list(seq)]) is None
