"""harness_hash — the behaviour-only, default-filling capability key hash (L1-A)."""

from __future__ import annotations

from agora.bench.keys import HARNESS_HASH_LEN, harness_hash, harness_hash_inputs


def test_empty_harness_is_all_defaults() -> None:
    # No harness == every behaviour field at its documented default.
    assert harness_hash(None) == harness_hash({})
    assert harness_hash({}) == harness_hash(
        {"tool_errors": "raw", "nudge_budget": 0, "review_budget": 0,
         "salvage_budget": 0, "routed_retry_budget": 2, "max_task_retries": 2}
    )


def test_hash_is_short_hex() -> None:
    h = harness_hash({"nudge_budget": 1})
    assert len(h) == HARNESS_HASH_LEN and all(c in "0123456789abcdef" for c in h)


def test_behaviour_change_changes_hash() -> None:
    base = harness_hash({"tool_errors": "raw", "nudge_budget": 0})
    assert harness_hash({"tool_errors": "corrective", "nudge_budget": 0}) != base
    assert harness_hash({"tool_errors": "raw", "nudge_budget": 1}) != base


def test_unrelated_keys_are_ignored() -> None:
    # Keys outside the behaviour set (endpoints, paths, telemetry) never affect it.
    a = harness_hash({"nudge_budget": 1})
    b = harness_hash({"nudge_budget": 1, "ollama_base_url": "http://x", "note": "hi"})
    assert a == b


def test_int_string_coercion_matches_typed() -> None:
    # An env-string budget hashes the same as the typed int (one canonical form).
    assert harness_hash({"nudge_budget": "1"}) == harness_hash({"nudge_budget": 1})
    assert harness_hash({"max_task_retries": "3"}) == harness_hash({"max_task_retries": 3})


def test_two_battery_arms_differ() -> None:
    prod = {"tool_errors": "corrective", "nudge_budget": 1, "review_budget": 0}
    raw = {"tool_errors": "raw", "nudge_budget": 0, "review_budget": 0}
    assert harness_hash(prod) != harness_hash(raw)


def test_inputs_expose_canonical_dict() -> None:
    got = harness_hash_inputs({"tool_errors": "corrective", "nudge_budget": "2"})
    assert got["tool_errors"] == "corrective"
    assert got["nudge_budget"] == 2  # coerced
    assert got["max_task_retries"] == 2  # default filled
    assert set(got) == {
        "tool_errors", "nudge_budget", "review_budget",
        "salvage_budget", "routed_retry_budget", "max_task_retries",
    }
