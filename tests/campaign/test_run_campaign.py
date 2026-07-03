"""Unit tests for the campaign harness: pure functions, resume, eviction protocol."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from scripts.expand_campaign import axis1_campaign
from scripts.run_campaign import (
    EvictionTimeout,
    OllamaControl,
    build_env,
    build_probe_command,
    expand_plan,
    load_campaign,
    maybe_evict,
    resume_filter,
    scan_done,
)

COMMITTED = "campaigns/axis-1-tool-call-fidelity.yaml"


# --------------------------------------------------------------- expand / params


def test_expand_plan_merges_default_params() -> None:
    plan = expand_plan(load_campaign(COMMITTED))
    assert len(plan) == 36
    # Every run inherits the campaign defaults.params.
    assert all(
        r["params"] == {"temperature": 0.0, "seed": 42, "num_ctx": 8192, "max_tokens": 2048}
        for r in plan
    )


def test_expand_plan_per_run_override(tmp_path) -> None:
    yaml_text = """
schema_version: 1
name: t
defaults:
  params: {temperature: 0.0, seed: 42, num_ctx: 8192, max_tokens: 2048}
  output_dir: out
  resume: true
runs:
  - {id: r001, probe: flows/tool-call-fidelity.plan.yaml, profile: qwen-coder-7b, arm: {scaffolding: lean, strictness: strict}, repeat: 1, params: {seed: 7}}
"""
    p = tmp_path / "c.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    plan = expand_plan(load_campaign(p))
    assert plan[0]["params"]["seed"] == 7  # overridden
    assert plan[0]["params"]["num_ctx"] == 8192  # inherited


def test_dry_run_plan_matches_committed_yaml() -> None:
    """The expanded plan from the committed YAML matches the generator output."""
    plan = expand_plan(load_campaign(COMMITTED))
    gen = axis1_campaign()["runs"]
    assert [r["id"] for r in plan] == [r["id"] for r in gen]
    assert [(r["profile"], r["arm"], r["repeat"]) for r in plan] == [
        (r["profile"], r["arm"], r["repeat"]) for r in gen
    ]


# --------------------------------------------------------------- resume


def test_resume_filter_removes_done() -> None:
    plan = expand_plan(load_campaign(COMMITTED))
    pending = resume_filter(plan, {"r001", "r002", "r003"})
    assert len(pending) == 33
    assert {"r001", "r002", "r003"}.isdisjoint({r["id"] for r in pending})


def test_scan_done_then_resume_shows_33_pending(tmp_path) -> None:
    """Pre-seed 3 per-run run.jsonl records; scan+filter ⇒ 33 pending."""
    plan = expand_plan(load_campaign(COMMITTED))
    for rid in ("r001", "r002", "r003"):
        d = tmp_path / rid
        d.mkdir(parents=True)
        (d / "run.jsonl").write_text(
            json.dumps({"schema_version": 1, "run_id": rid}) + "\n", encoding="utf-8"
        )
    done = scan_done(tmp_path)
    assert done == {"r001", "r002", "r003"}
    assert len(resume_filter(plan, done)) == 33


def test_scan_done_ignores_dirs_without_valid_run_jsonl(tmp_path) -> None:
    (tmp_path / "r001").mkdir()  # no run.jsonl
    (tmp_path / "r002").mkdir()
    (tmp_path / "r002" / "run.jsonl").write_text("not json\n", encoding="utf-8")
    assert scan_done(tmp_path) == set()


# --------------------------------------------------------------- env construction


def test_build_env_maps_profile_and_params() -> None:
    run = {
        "id": "r001",
        "profile": "qwen-coder-7b",
        "params": {"temperature": 0.0, "seed": 42, "num_ctx": 8192, "max_tokens": 2048},
    }
    env = build_env(run, "runs_out/x/r001")
    assert env["AGORA_PROFILE"] == "qwen-coder-7b"
    assert env["AGORA_RUN_OUTPUT_DIR"] == "runs_out/x/r001"
    assert env["AGORA_LLM_TEMPERATURE"] == "0.0"
    assert env["AGORA_LLM_SEED"] == "42"
    assert env["AGORA_LLM_NUM_CTX"] == "8192"
    assert env["AGORA_LLM_MAX_TOKENS"] == "2048"


def test_build_env_propagates_arm() -> None:
    """The per-run arm must reach the probe runner (else run.jsonl defaults to
    rich/strict for every run and the lean/rich dimension is lost)."""
    run = {
        "id": "r001", "profile": "qwen-coder-7b", "params": {},
        "arm": {"scaffolding": "lean", "strictness": "strict"},
    }
    env = build_env(run, "runs_out/x/r001")
    assert env["AGORA_ARM_SCAFFOLDING"] == "lean"
    assert env["AGORA_ARM_STRICTNESS"] == "strict"
    # No arm key → no arm env (standalone / degraded run).
    assert "AGORA_ARM_SCAFFOLDING" not in build_env(
        {"id": "r1", "profile": "p", "params": {}}, "d"
    )


def test_build_env_forwards_review_timeout() -> None:
    run = {"id": "r001", "profile": "p", "params": {}, "review_timeout_seconds": 5}
    env = build_env(run, "d")
    assert env["AGORA_REVIEW_TIMEOUT_SECONDS"] == "5"
    # None / absent → not forwarded (runner keeps its own default).
    assert "AGORA_REVIEW_TIMEOUT_SECONDS" not in build_env(
        {"id": "r", "profile": "p", "params": {}, "review_timeout_seconds": None}, "d"
    )
    assert "AGORA_REVIEW_TIMEOUT_SECONDS" not in build_env(
        {"id": "r", "profile": "p", "params": {}}, "d"
    )


def test_build_env_emits_strategy_when_set() -> None:
    """A per-run strategy reaches the probe runner via AGORA_STRATEGY; a control
    cell (strategy None/absent) emits nothing so the runner builds no wrapper."""
    run = {"id": "r001", "profile": "p", "params": {}, "strategy": "qwen2_5_coder"}
    assert build_env(run, "d")["AGORA_STRATEGY"] == "qwen2_5_coder"
    assert "AGORA_STRATEGY" not in build_env(
        {"id": "r", "profile": "p", "params": {}, "strategy": None}, "d"
    )
    assert "AGORA_STRATEGY" not in build_env(
        {"id": "r", "profile": "p", "params": {}}, "d"
    )


def test_load_campaign_rejects_unknown_strategy(tmp_path) -> None:
    """An unknown strategy name fails at load — not at run 23 of 40."""
    yaml_text = """
schema_version: 1
name: bad
defaults:
  params: {seed: 42}
  output_dir: out
runs:
  - {id: r001, probe: flows/tool-call-fidelity.plan.yaml, profile: gemma-e4b, arm: {scaffolding: lean, strictness: strict}, repeat: 1, strategy: nonexistent_strategy}
"""
    camp = tmp_path / "bad.yaml"
    camp.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown strategy"):
        load_campaign(str(camp))


def test_load_campaign_accepts_known_strategy_and_expands(tmp_path) -> None:
    """A registered strategy loads and survives expand_plan onto the run dict."""
    yaml_text = """
schema_version: 1
name: ok
defaults:
  params: {seed: 42}
  output_dir: out
runs:
  - {id: r001, probe: flows/tool-call-fidelity.plan.yaml, profile: qwen-coder-7b, arm: {scaffolding: lean, strictness: strict}, repeat: 1, strategy: qwen2_5_coder}
  - {id: r002, probe: flows/tool-call-fidelity.plan.yaml, profile: qwen-coder-7b, arm: {scaffolding: lean, strictness: strict}, repeat: 1}
"""
    camp = tmp_path / "ok.yaml"
    camp.write_text(yaml_text, encoding="utf-8")
    plan = expand_plan(load_campaign(str(camp)))
    assert plan[0]["strategy"] == "qwen2_5_coder"
    assert plan[1]["strategy"] is None  # control cell


def test_harness_defaults_field_merge_and_env(tmp_path) -> None:
    """v3 harness config: defaults apply, per-run field-merges over them, and
    build_env emits AGORA_HARNESS_* from the resolved dict."""
    yaml_text = """
schema_version: 1
name: t
defaults:
  params: {seed: 42}
  output_dir: out
  harness: {tool_errors: corrective, nudge_budget: 1}
runs:
  - {id: r001, probe: flows/tool-call-fidelity.plan.yaml, profile: gemma-e4b, arm: {scaffolding: lean, strictness: strict}, repeat: 1}
  - {id: r002, probe: flows/tool-call-fidelity.plan.yaml, profile: gemma-e4b, arm: {scaffolding: lean, strictness: strict}, repeat: 1, harness: {nudge_budget: 5}}
"""
    camp = tmp_path / "c.yaml"
    camp.write_text(yaml_text, encoding="utf-8")
    plan = expand_plan(load_campaign(str(camp)))
    assert plan[0]["harness"] == {"tool_errors": "corrective", "nudge_budget": 1}
    # per-run overrides only nudge_budget; tool_errors inherited from defaults
    assert plan[1]["harness"] == {"tool_errors": "corrective", "nudge_budget": 5}
    env = build_env(plan[0], "d")
    assert env["AGORA_HARNESS_TOOL_ERRORS"] == "corrective"
    assert env["AGORA_HARNESS_NUDGE_BUDGET"] == "1"


def test_load_campaign_rejects_invalid_tool_errors(tmp_path) -> None:
    """Invalid harness values fail loudly at load (pydantic Literal)."""
    yaml_text = """
schema_version: 1
name: bad
defaults:
  params: {seed: 42}
  output_dir: out
  harness: {tool_errors: bogus}
runs:
  - {id: r001, probe: flows/tool-call-fidelity.plan.yaml, profile: gemma-e4b, arm: {scaffolding: lean, strictness: strict}, repeat: 1}
"""
    camp = tmp_path / "bad.yaml"
    camp.write_text(yaml_text, encoding="utf-8")
    with pytest.raises((ValueError, ValidationError)):
        load_campaign(str(camp))


def test_v2_campaign_defaults_to_raw_no_nudge() -> None:
    """A campaign with no harness block resolves to raw/0 — v2 behaviour — and
    the committed v2 YAML still loads."""
    plan = expand_plan(load_campaign(COMMITTED))
    assert all(r["harness"] == {"tool_errors": "raw", "nudge_budget": 0} for r in plan)


def test_expand_plan_carries_review_timeout_from_defaults() -> None:
    plan = expand_plan(load_campaign(COMMITTED))
    assert all(r["review_timeout_seconds"] == 5 for r in plan)


def test_build_probe_command_targets_registered_runner() -> None:
    run = {
        "id": "r001",
        "probe": "flows/tool-call-fidelity.plan.yaml",
        "profile": "qwen-coder-7b",
        "params": {"seed": 42},
    }
    cmd = build_probe_command(run, "runs_out/x/r001")
    joined = " ".join(cmd)
    assert "probe_model_lifecycle.py" in joined
    assert "run_tool_call_fidelity.py" in joined
    assert "AGORA_PROFILE=qwen-coder-7b" in cmd
    assert "--out" in cmd


def test_build_probe_command_unknown_probe_raises() -> None:
    run = {"id": "r1", "probe": "flows/nope.yaml", "profile": "p", "params": {}}
    with pytest.raises(ValueError, match="no runner registered"):
        build_probe_command(run, "x")


# --------------------------------------------------------------- eviction protocol


class FakeControl:
    """Records calls; list_running reflects evict/prewarm mutations."""

    def __init__(self, running=(), *, evict_works=True):
        self.running = set(running)
        self.evict_works = evict_works
        self.calls: list[tuple] = []

    def evict(self, model):
        self.calls.append(("evict", model))
        if self.evict_works:
            self.running.discard(model)

    def prewarm(self, model, keep_alive, *, num_ctx=None):
        self.calls.append(("prewarm", model, keep_alive, num_ctx))
        self.running.add(model)

    def list_running(self):
        self.calls.append(("ps",))
        return set(self.running)


def test_eviction_on_model_change_runs_three_steps() -> None:
    ctl = FakeControl(running={"m_a"})
    ran = maybe_evict("m_a", "m_b", ctl, "30m", sleep_fn=lambda *_: None)
    assert ran is True
    kinds = [c[0] for c in ctl.calls]
    # evict outgoing → poll (ps) → prewarm incoming → verify (ps)
    assert kinds[0] == "evict" and ctl.calls[0][1] == "m_a"
    assert "prewarm" in kinds
    prewarm_call = next(c for c in ctl.calls if c[0] == "prewarm")
    assert prewarm_call[1] == "m_b" and prewarm_call[2] == "30m"
    # evict precedes prewarm.
    assert kinds.index("evict") < kinds.index("prewarm")


def test_eviction_prewarms_with_num_ctx() -> None:
    """The pinned num_ctx must reach prewarm so the model loads at the right
    context (else the block-first run runs at the model default)."""
    ctl = FakeControl(running={"m_a"})
    maybe_evict("m_a", "m_b", ctl, "30m", num_ctx=8192, sleep_fn=lambda *_: None)
    prewarm_call = next(c for c in ctl.calls if c[0] == "prewarm")
    assert prewarm_call[1] == "m_b"
    assert prewarm_call[3] == 8192  # num_ctx threaded through


def test_prewarm_payload_carries_num_ctx(monkeypatch) -> None:
    seen: dict = {}
    ctl = OllamaControl()
    monkeypatch.setattr(ctl, "_post", lambda path, payload, **k: seen.update(payload=payload))
    ctl.prewarm("m", "30m", num_ctx=8192)
    assert seen["payload"]["options"] == {"num_ctx": 8192}
    # no num_ctx → no options key (Ollama uses the model default)
    seen.clear()
    ctl.prewarm("m", "30m")
    assert "options" not in seen["payload"]


def test_eviction_skipped_for_same_model() -> None:
    ctl = FakeControl(running={"m_a"})
    ran = maybe_evict("m_a", "m_a", ctl, "30m", sleep_fn=lambda *_: None)
    assert ran is False
    assert ctl.calls == []  # no evict, no prewarm, no poll


def test_eviction_first_run_only_prewarms() -> None:
    """prev_model is None (campaign start) ⇒ no evict, just pre-warm + verify."""
    ctl = FakeControl(running=set())
    # prewarm adds m_a so residency check passes.
    ran = maybe_evict(None, "m_a", ctl, "30m", sleep_fn=lambda *_: None)
    assert ran is True
    assert not any(c[0] == "evict" for c in ctl.calls)
    assert any(c[0] == "prewarm" for c in ctl.calls)


def test_eviction_timeout_when_model_wont_unload() -> None:
    ctl = FakeControl(running={"m_a"}, evict_works=False)  # stays resident
    times = iter([0, 10, 20, 30, 31, 40])
    with pytest.raises(EvictionTimeout):
        maybe_evict(
            "m_a", "m_b", ctl, "30m",
            poll_cap=30, sleep_fn=lambda *_: None, now_fn=lambda: next(times),
        )


# --------------------------------------------------------------- dry-run end to end


def test_run_campaign_dry_run_offline(monkeypatch, capsys) -> None:
    """--dry-run validates + prints 36 tuples + exits 0 without touching Ollama."""
    from scripts.run_campaign import run_campaign

    rc = run_campaign(COMMITTED, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "36 runs" in out
    assert out.count("probe=flows/tool-call-fidelity.plan.yaml") == 36


@pytest.mark.slow
@pytest.mark.skipif(
    __import__("os").getenv("AGORA_E2E") != "1",
    reason="AGORA_E2E=1 gates the live 1-run campaign smoke",
)
def test_campaign_one_run_end_to_end(tmp_path) -> None:
    """A tiny 1-run campaign launches a real probe child and emits JSONL.

    Requires live Conduit + Ollama with qwen2.5:7b-instruct present.
    """
    from scripts.run_campaign import run_campaign

    out_dir = tmp_path / "camp"
    yaml_text = f"""
schema_version: 1
name: smoke
defaults:
  params: {{temperature: 0.0, seed: 42, num_ctx: 8192, max_tokens: 2048}}
  output_dir: {out_dir.as_posix()}
  resume: true
runs:
  - {{id: r001, probe: flows/tool-call-fidelity.plan.yaml, profile: qwen-instruct-7b, arm: {{scaffolding: lean, strictness: strict}}, repeat: 1}}
"""
    camp = tmp_path / "smoke.yaml"
    camp.write_text(yaml_text, encoding="utf-8")
    rc = run_campaign(camp)
    assert rc == 0
    assert (out_dir / "plan.jsonl").is_file()
    assert (out_dir / "run.jsonl").is_file()
    assert (out_dir / "r001" / "run.jsonl").is_file()
