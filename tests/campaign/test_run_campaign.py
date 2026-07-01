"""Unit tests for the campaign harness: pure functions, resume, eviction protocol."""

from __future__ import annotations

import json

import pytest

from scripts.expand_campaign import axis1_campaign
from scripts.run_campaign import (
    EvictionTimeout,
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

    def prewarm(self, model, keep_alive):
        self.calls.append(("prewarm", model, keep_alive))
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
