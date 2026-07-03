"""Guards for scripts/analyze_layer2.py (axis-1 v2 post-campaign hardening)."""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.analyze_layer2 import _steady_tasks


def _tasks(cids: list, models: list | None = None) -> pd.DataFrame:
    models = models or ["m"] * len(cids)
    return pd.DataFrame(
        {
            "model": models,
            "campaign_run_id": pd.array(cids, dtype="string"),
            "task_id": ["small_chain"] * len(cids),
        }
    )


def test_steady_tasks_raises_loudly_on_null_campaign_run_id() -> None:
    """A null campaign_run_id means plan.jsonl was incomplete; _steady_tasks must
    name the count and cause instead of letting pandas raise an opaque KeyError."""
    tasks = _tasks(["r001", None])
    runs = pd.DataFrame({"model": ["m"], "campaign_run_id": pd.array(["r001"], dtype="string")})
    with pytest.raises(ValueError, match=r"1 task row\(s\) have a null campaign_run_id"):
        _steady_tasks(tasks, runs)
    with pytest.raises(ValueError, match="findings C1"):
        _steady_tasks(tasks, runs)


def test_steady_tasks_drops_block_first_when_ids_present() -> None:
    """With all ids present the guard passes and the block-first run is dropped."""
    tasks = _tasks(["r001", "r002"])
    runs = pd.DataFrame(
        {"model": ["m", "m"], "campaign_run_id": pd.array(["r001", "r002"], dtype="string")}
    )
    out = _steady_tasks(tasks, runs)
    assert list(out["campaign_run_id"]) == ["r002"]  # r001 = block-first, excluded
