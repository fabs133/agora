"""Unit tests for agora.doctor — the one preflight module (Stage 4).

Network is monkeypatched at the module's small seams (_get_json, check_model_fits,
AgoraMatrixClient) so each check's red/green verdict and the report exit code are
deterministic without a live stack.
"""

from __future__ import annotations

import pytest

from agora import doctor
from agora.doctor import CheckResult

# ------------------------------------------------------------- pure helpers


def test_ollama_missing_models() -> None:
    tags = {"models": [{"name": "gemma4:e4b"}, {"name": "nomic-embed-text:latest"}]}
    assert doctor.ollama_missing_models(tags, ["gemma4:e4b"]) == []
    assert doctor.ollama_missing_models(
        tags, ["gemma4:e4b", "qwen2.5:7b-instruct"]
    ) == ["qwen2.5:7b-instruct"]


def test_format_line_shows_hint_only_on_red() -> None:
    assert "→" not in doctor.format_line(CheckResult("x", True, "fine", hint="unused"))
    line = doctor.format_line(CheckResult("x", False, "broken", hint="do the thing"))
    assert "FAIL" in line and "do the thing" in line


def test_report_exit_codes() -> None:
    green = [CheckResult("a", True, "ok"), CheckResult("b", True, "ok")]
    red = [CheckResult("a", True, "ok"), CheckResult("b", False, "bad")]
    assert doctor.report(green, echo=lambda _s: None) == 0
    assert doctor.report(red, echo=lambda _s: None) == 1


def test_preflight_or_die_raises_on_red() -> None:
    with pytest.raises(SystemExit):
        doctor.preflight_or_die([CheckResult("a", False, "bad")], echo=lambda _s: None)
    # All-green does not raise.
    doctor.preflight_or_die([CheckResult("a", True, "ok")], echo=lambda _s: None)


# ------------------------------------------------------------- ollama checks


def test_check_ollama_reachable_green(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_get_json", lambda url, t: {"version": "0.1.2"})
    r = doctor.check_ollama_reachable("http://ol.test:11434")
    assert r.ok and "0.1.2" in r.detail


def test_check_ollama_reachable_red(monkeypatch) -> None:
    def _boom(url, t):
        raise OSError("refused")

    monkeypatch.setattr(doctor, "_get_json", _boom)
    r = doctor.check_ollama_reachable("http://ol.test:11434")
    assert not r.ok and r.hint


def test_check_ollama_models_missing(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_get_json", lambda url, t: {"models": [{"name": "a:1"}]})
    r = doctor.check_ollama_models("http://ol.test:11434", ["a:1", "b:2"])
    assert not r.ok and "b:2" in r.detail and "ollama pull" in r.hint


def test_check_ollama_models_empty_is_green(monkeypatch) -> None:
    r = doctor.check_ollama_models("http://ol.test:11434", [])
    assert r.ok


# ------------------------------------------------------------- vram check


async def test_check_vram_green_when_fits(monkeypatch) -> None:
    from agora.fleet.vram import VRAMCheck

    async def _fits(model, base_url, safety_margin_mib):
        return VRAMCheck(fits=True, free_mib=20000, required_mib=5000, reason="fits")

    monkeypatch.setattr("agora.fleet.vram.check_model_fits", _fits)
    r = await doctor.check_vram("ollama/m", "http://ol.test:11434", 512)
    assert r.ok


async def test_check_vram_red_when_wont_fit(monkeypatch) -> None:
    from agora.fleet.vram import VRAMCheck

    async def _no(model, base_url, safety_margin_mib):
        return VRAMCheck(fits=False, free_mib=1000, required_mib=20000, reason="too big")

    monkeypatch.setattr("agora.fleet.vram.check_model_fits", _no)
    r = await doctor.check_vram("ollama/m", "http://ol.test:11434", 512)
    assert not r.ok and r.hint


async def test_check_vram_probe_failure_is_green(monkeypatch) -> None:
    async def _boom(model, base_url, safety_margin_mib):
        raise OSError("no daemon")

    monkeypatch.setattr("agora.fleet.vram.check_model_fits", _boom)
    r = await doctor.check_vram("ollama/m", "http://ol.test:11434", 512)
    assert r.ok  # never block on an unknown


# ------------------------------------------------------------- conduit check


async def test_check_conduit_no_password_is_red() -> None:
    r = await doctor.check_conduit("http://hs.test:6167", "@agora:test", "")
    assert not r.ok and "AGORA_MATRIX_PASSWORD" in r.hint


async def test_check_conduit_login_ok(monkeypatch) -> None:
    class _FakeClient:
        def __init__(self, **_kw):
            pass

        async def login(self, _pw):
            return None

        async def close(self):
            return None

    monkeypatch.setattr("agora.matrix.client.AgoraMatrixClient", _FakeClient)
    r = await doctor.check_conduit("http://hs.test:6167", "@agora:test", "pw")
    assert r.ok


async def test_check_conduit_login_timeout_is_red(monkeypatch) -> None:
    """A down homeserver must produce a fast red line, never hang (Stage 6)."""

    class _FakeClient:
        def __init__(self, **_kw):
            pass

        async def login(self, _pw):
            raise TimeoutError

        async def close(self):
            return None

    monkeypatch.setattr("agora.matrix.client.AgoraMatrixClient", _FakeClient)
    r = await doctor.check_conduit("http://hs.test:6167", "@agora:test", "pw")
    assert not r.ok and "timed out" in r.detail


async def test_check_conduit_login_failure_is_red(monkeypatch) -> None:
    class _FakeClient:
        def __init__(self, **_kw):
            pass

        async def login(self, _pw):
            raise ConnectionError("down")

        async def close(self):
            return None

    monkeypatch.setattr("agora.matrix.client.AgoraMatrixClient", _FakeClient)
    r = await doctor.check_conduit("http://hs.test:6167", "@agora:test", "pw")
    assert not r.ok


# ------------------------------------------------------------- workspace check


def test_check_workspace_git_green(tmp_path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    r = doctor.check_workspace_git(tmp_path, tmp_path / "workspace")
    assert r.ok
    assert (tmp_path / "workspace").is_dir()


def test_check_workspace_git_red_outside_repo(tmp_path) -> None:
    r = doctor.check_workspace_git(tmp_path, tmp_path / "workspace")
    assert not r.ok  # tmp_path is not a git work tree


# ------------------------------------------------------------- orchestration


async def test_run_checks_conduit_only_when_homeserver_given(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_get_json", lambda url, t: {"version": "x", "models": []})

    async def _fits(model, base_url, safety_margin_mib):
        from agora.fleet.vram import VRAMCheck

        return VRAMCheck(fits=True, free_mib=1, required_mib=0, reason="ok")

    monkeypatch.setattr("agora.fleet.vram.check_model_fits", _fits)
    names = {
        r.name
        for r in await doctor.run_checks(
            ollama_base_url="http://ol.test:11434",
            vram_model="ollama/m",
            vram_safety_margin_mib=512,
            repo_root=".",
            work_dir="workspace",
        )
    }
    assert "conduit" not in names  # no homeserver ⇒ skipped
    assert {"ollama", "ollama-models", "vram", "workspace"} <= names
