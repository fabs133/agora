from agora.core.contract import (
    Specification,
    evaluate_postconditions,
    evaluate_preconditions,
    make_predicate,
)


def _pred_true(name: str = "always") -> "make_predicate":
    return make_predicate(name, f"{name} description", lambda ctx: (True, ""))


def _pred_false(name: str = "fail") -> "make_predicate":
    return make_predicate(name, f"{name} description", lambda ctx: (False, f"{name} missing"))


def test_predicate_passes() -> None:
    pred = _pred_true("has_input")
    passed, reason = pred.evaluate({})
    assert passed is True
    assert reason == ""


def test_predicate_fails_with_reason() -> None:
    pred = _pred_false("needs_x")
    passed, reason = pred.evaluate({})
    assert passed is False
    assert "needs_x missing" in reason


def test_specification_fingerprint_deterministic() -> None:
    spec_a = Specification(
        preconditions=(_pred_true("a"),),
        postconditions=(_pred_true("b"),),
        description="same",
    )
    spec_b = Specification(
        preconditions=(_pred_true("a"),),
        postconditions=(_pred_true("b"),),
        description="same",
    )
    assert spec_a.fingerprint == spec_b.fingerprint


def test_specification_fingerprint_changes() -> None:
    spec_a = Specification(description="one")
    spec_b = Specification(description="two")
    assert spec_a.fingerprint != spec_b.fingerprint


def test_specification_fingerprint_is_order_independent_for_predicates() -> None:
    p1 = _pred_true("a")
    p2 = _pred_true("b")
    spec_a = Specification(preconditions=(p1, p2), description="x")
    spec_b = Specification(preconditions=(p2, p1), description="x")
    assert spec_a.fingerprint == spec_b.fingerprint


def test_evaluate_preconditions_all_pass() -> None:
    spec = Specification(preconditions=(_pred_true("a"), _pred_true("b")))
    assert evaluate_preconditions(spec, {}) == []


def test_evaluate_preconditions_some_fail() -> None:
    spec = Specification(preconditions=(_pred_true("ok"), _pred_false("bad")))
    failures = evaluate_preconditions(spec, {})
    assert len(failures) == 1
    assert failures[0][0] == "bad"
    assert "missing" in failures[0][1]


def test_evaluate_postconditions_all_pass() -> None:
    spec = Specification(postconditions=(_pred_true("a"),))
    assert evaluate_postconditions(spec, {"result": "ok"}) == []


def test_evaluate_postconditions_some_fail() -> None:
    spec = Specification(postconditions=(_pred_false("missing_output"),))
    failures = evaluate_postconditions(spec, {})
    assert failures == [("missing_output", "missing_output missing")]
