"""v3 harness-reliability mechanisms: tool-boundary contract (S1) + capture (S4)."""

from __future__ import annotations

from agora.fleet.agent_runtime import (
    CorrectiveError,
    _capture_failed_artifact,
    _dispatch_tool,
    _run_tool,
    validate_call,
)
from agora.fleet.llm_adapter import ToolCall

_MARK_COMPLETE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="0", name=name, arguments=args)


# --------------------------------------------------------------------- validate_call


def test_validate_call_happy_path() -> None:
    assert validate_call(_call("mark_complete", {"summary": "x"}), _MARK_COMPLETE_SCHEMA) is None
    # No schema ⇒ nothing to validate.
    assert validate_call(_call("whatever", {}), None) is None


def test_validate_call_missing_required_returns_corrective() -> None:
    ce = validate_call(
        _call("mark_complete", {"path": "a", "content": "b"}), _MARK_COMPLETE_SCHEMA
    )
    assert isinstance(ce, CorrectiveError)
    rendered = ce.render()
    assert "missing required argument(s): summary" in rendered
    assert "Expected schema for mark_complete" in rendered
    assert "write_file(path, content)" in rendered  # the one hard-coded hint
    # The whole point: no raw crash text ever reaches the model.
    assert "Traceback" not in rendered and "KeyError" not in rendered


# --------------------------------------------------------------------- _dispatch_tool


async def test_dispatch_raw_is_byte_identical_to_v2_crash_string() -> None:
    async def boom(_args):
        raise KeyError("summary")

    executor = {"mark_complete": boom}
    call = _call("mark_complete", {"path": "a", "content": "b"})
    raw = await _dispatch_tool(call, executor, schema=_MARK_COMPLETE_SCHEMA, mode="raw")
    # Exactly the v2 string, and exactly what the legacy _run_tool produces.
    assert raw == "ERROR: tool mark_complete raised: 'summary'"
    assert raw == await _run_tool(call, executor)


async def test_dispatch_corrective_rejects_before_handler_runs() -> None:
    ran = False

    async def boom(_args):
        nonlocal ran
        ran = True
        raise KeyError("summary")

    executor = {"mark_complete": boom}
    call = _call("mark_complete", {"path": "a", "content": "b"})
    out = await _dispatch_tool(call, executor, schema=_MARK_COMPLETE_SCHEMA, mode="corrective")
    assert ran is False  # validation caught the bad call before dispatch
    assert "was rejected" in out and "Hint:" in out
    assert "Traceback" not in out and "KeyError" not in out


async def test_dispatch_corrective_renders_handler_exception() -> None:
    async def boom(_args):
        raise RuntimeError("disk full")

    executor = {"read_file": boom}
    call = _call("read_file", {"path": "a"})  # valid args ⇒ handler runs and raises
    out = await _dispatch_tool(
        call, executor, schema={"required": ["path"]}, mode="corrective"
    )
    assert "raised while running: disk full" in out
    assert "Traceback" not in out


# --------------------------------------------------------------------- artifact_capture


def test_capture_failed_artifact_truncates_at_2kb(tmp_path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "c.txt").write_bytes(b"ab" * 2000)  # 4000 bytes
    cap = _capture_failed_artifact("out/c.txt", success=False, work_dir=str(tmp_path))
    assert cap["path"] == "out/c.txt"
    assert cap["size_bytes"] == 4000
    assert cap["truncated"] is True
    assert len(cap["text"]) == 2048


def test_capture_failed_artifact_absent_on_pass_and_when_unwritten(tmp_path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "c.txt").write_bytes(b"hi")
    # Pass ⇒ nothing captured.
    assert _capture_failed_artifact("out/c.txt", success=True, work_dir=str(tmp_path)) is None
    # Failure but output never written ⇒ nothing to diff.
    assert _capture_failed_artifact("out/missing.txt", success=False, work_dir=str(tmp_path)) is None
    # Small written file on failure ⇒ captured, not truncated.
    cap = _capture_failed_artifact("out/c.txt", success=False, work_dir=str(tmp_path))
    assert cap["truncated"] is False and cap["text"] == "hi"
