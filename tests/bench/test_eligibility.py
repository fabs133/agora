"""Role eligibility over the matrix (L1-C)."""

from __future__ import annotations

import pandas as pd

from agora.bench.eligibility import eligible_digests, evaluate_role
from agora.bench.keys import harness_hash
from agora.bench.matrix import MATRIX_COLUMNS
from agora.fleet.roles import load_roles

_IMPL = load_roles("roles.yaml").role("implementer")
_HH = harness_hash(_IMPL.harness)


def _row(digest, sub_target, raw, *, hh=_HH, pv=7):
    r = dict.fromkeys(MATRIX_COLUMNS)
    r.update(
        model_digest=digest, battery_version="standard-v1", probe_version=pv,
        harness_hash=hh, daemon_version="0.1.0", model="gemma-e4b", strategy=None,
        axis="tool_call_fidelity", sub_target=sub_target, raw_value=raw,
        date="2026-07-01", source="local",
    )
    return r


def _matrix(rows):
    return pd.DataFrame(rows).reindex(columns=list(MATRIX_COLUMNS))


def test_eligible_when_all_thresholds_met() -> None:
    m = _matrix([
        _row("sha:pass", "pass_rate", 1.0),
        _row("sha:pass", "trajectory_reproducibility_rate", 1.0),
    ])
    res = evaluate_role(m, _IMPL, probe_version=7)
    assert len(res) == 1 and res[0].eligible
    assert eligible_digests(m, _IMPL) == {"sha:pass"}


def test_ineligible_when_pass_rate_below_threshold() -> None:
    m = _matrix([
        _row("sha:fail", "pass_rate", 0.66),
        _row("sha:fail", "trajectory_reproducibility_rate", 1.0),
    ])
    res = evaluate_role(m, _IMPL)
    assert not res[0].eligible
    assert any("pass_rate" in f for f in res[0].failures)
    assert eligible_digests(m, _IMPL) == set()


def test_ineligible_when_not_reproducible() -> None:
    m = _matrix([
        _row("sha:x", "pass_rate", 1.0),
        _row("sha:x", "trajectory_reproducibility_rate", 0.66),
    ])
    res = evaluate_role(m, _IMPL)
    assert not res[0].eligible
    assert any("reproducibility" in f for f in res[0].failures)


def test_rows_at_a_different_harness_key_do_not_count() -> None:
    m = _matrix([
        _row("sha:pass", "pass_rate", 1.0, hh="deadbeefcafe"),
        _row("sha:pass", "trajectory_reproducibility_rate", 1.0, hh="deadbeefcafe"),
    ])
    assert evaluate_role(m, _IMPL) == []  # nothing measured at THIS role's harness


def test_unmeasured_role_yields_no_results() -> None:
    planner = load_roles("roles.yaml").role("planner")
    m = _matrix([_row("sha:x", "pass_rate", 1.0)])
    assert evaluate_role(m, planner) == []
