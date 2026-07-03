"""Tests for scripts/compare_campaigns.py (axis-1 v1/v2 comparison table)."""

from __future__ import annotations

from scripts.compare_campaigns import build_table


def _row(model, strategy, sub, raw):
    return {"model": model, "strategy": strategy, "sub_target": sub, "raw_value": raw}


def test_build_table_pivots_three_columns() -> None:
    v1 = [_row("coder-7b", "", "pass_rate", "0.0"),
          _row("coder-7b", "", "text_fallback_rate", "1.0")]
    v2 = [_row("coder-7b", "", "pass_rate", "0.0"),
          _row("coder-7b", "qwen2_5_coder", "pass_rate", "0.0"),
          _row("coder-7b", "", "text_fallback_rate", "1.0"),
          _row("coder-7b", "qwen2_5_coder", "text_fallback_rate", "0.0")]
    table = build_table(v1, v2)
    assert "| model | sub_target | v1-steady | v2-control | v2-treatment |" in table
    # treatment flipped text_fallback 1.0 -> 0.0; both arms present
    assert "| coder-7b | text_fallback_rate | 1 | 1 | 0 |" in table


def test_model_without_treatment_emits_dash() -> None:
    """A sentinel model has control but no treatment cells → the treatment
    column must render the em-dash placeholder, not blank or an error."""
    v1 = [_row("gemma-e4b", "", "pass_rate", "0.667")]
    v2 = [_row("gemma-e4b", "", "pass_rate", "0.667")]
    table = build_table(v1, v2)
    assert "| gemma-e4b | pass_rate | 0.667 | 0.667 | — |" in table
