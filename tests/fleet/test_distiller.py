"""Tests for the hierarchical map-reduce file distiller."""

from __future__ import annotations

from pathlib import Path

from agora.core.types import AgentRole
from agora.fleet.distiller import (
    _split_into_buckets,
    distill,
    make_distill_fn,
)
from agora.fleet.inner_tools import ToolContext, get_tool_executor
from agora.fleet.llm_adapter import LLMResponse
from tests.conftest import FakeLLM

# ----------------------------------------------------------- _split_into_buckets


def test_split_into_buckets_keeps_lines_together() -> None:
    text = "line one\nline two\nline three\nline four\n"
    buckets = _split_into_buckets(text, bucket_chars=20)
    # Every bucket ends on a newline — no line was split mid-sentence.
    assert all(b.endswith("\n") or b == buckets[-1] for b in buckets)
    # Lines reassemble exactly.
    assert "".join(buckets) == text


def test_split_into_buckets_respects_limit() -> None:
    text = "x" * 100
    buckets = _split_into_buckets(text + "\n", bucket_chars=25)
    # One long line longer than the bucket limit is sliced on byte boundary.
    assert all(len(b) <= 25 for b in buckets)
    assert "".join(buckets) == text + "\n"


def test_split_into_buckets_returns_whole_text_when_small() -> None:
    text = "short text\n"
    assert _split_into_buckets(text, bucket_chars=1000) == [text]


# ------------------------------------------------------------------- distill


async def test_distill_returns_source_unchanged_when_under_target() -> None:
    text = "small content\n"
    result = await distill(
        text,
        focus="anything",
        llm=FakeLLM([]),
        model="x",
        target_chars=1000,
    )
    assert result == text


async def test_distill_shrinks_large_text_via_bucket_extraction() -> None:
    # 40 KB of repetitive content.
    src = ("line about FOO\nline about BAR\n" * 1500)
    assert len(src) > 30_000

    # FakeLLM responds with a short distilled extract for each bucket call.
    responses = [LLMResponse(content="• FOO note\n• BAR note")] * 50
    llm = FakeLLM(responses)

    result = await distill(
        src,
        focus="FOO/BAR notes",
        llm=llm,
        model="x",
        target_chars=2000,
        bucket_chars=4000,
    )
    assert len(result) <= 2000 + 200  # target + annotation banner
    assert "FOO" in result
    assert "distilled from" in result  # annotation banner present


async def test_distill_skips_buckets_with_no_relevant_content() -> None:
    src = "irrelevant stuff\n" * 2000  # ~32 KB
    llm = FakeLLM([LLMResponse(content="(no relevant content)")] * 20)
    result = await distill(
        src,
        focus="FOO",
        llm=llm,
        model="x",
        target_chars=500,
        bucket_chars=2000,
    )
    # Every bucket said "no relevant content" — fall back to head-truncation.
    assert "truncated" in result.lower()
    assert len(result) <= 500 + 200


async def test_distill_falls_back_on_llm_error() -> None:
    src = "x" * 20_000

    class BoomLLM:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("simulated LLM outage")

        def format_assistant_turn(self, response): ...
        def format_tool_results(self, calls, results): ...

    result = await distill(
        src,
        focus="anything",
        llm=BoomLLM(),
        model="x",
        target_chars=500,
        bucket_chars=2000,
    )
    # All buckets errored → no output → fall back to truncation.
    assert "truncated" in result.lower()
    assert len(result) <= 500 + 200


async def test_distill_recurses_when_first_round_still_too_large() -> None:
    """Two rounds needed: round 1 reduces source → merged still too big → round 2 shrinks further."""
    src = "chapter X\n" * 5000  # ~50 KB
    # Round 1: 13 buckets × 500-char extract ≈ 6.5 KB merged — still over 3 KB target.
    # Round 2: 2 buckets × 100-char extract ≈ 200 chars — well under target.
    responses: list[LLMResponse] = []
    for _ in range(30):
        responses.append(LLMResponse(content="X fact " * 70))  # ~500 chars each
    for _ in range(30):
        responses.append(LLMResponse(content="Y sum"))
    llm = FakeLLM(responses)
    result = await distill(
        src,
        focus="chapters",
        llm=llm,
        model="x",
        target_chars=3_000,
        bucket_chars=4_000,
        max_rounds=3,
    )
    assert len(result) <= 3_000 + 200
    assert "distilled from" in result


async def test_distill_caps_recursion_at_max_rounds() -> None:
    src = "padding\n" * 10_000  # ~80 KB
    # LLM always returns content that's the same size as its input.
    class NoShrinkLLM:
        async def complete(self, *args, **kwargs):
            return LLMResponse(content="padding\n" * 400)

        def format_assistant_turn(self, response): ...
        def format_tool_results(self, calls, results): ...

    result = await distill(
        src,
        focus="anything",
        llm=NoShrinkLLM(),
        model="x",
        target_chars=500,
        bucket_chars=2_000,
        max_rounds=2,
    )
    assert "max-rounds" in result
    assert len(result) <= 500 + 200


# -------------------------------------------------- make_distill_fn + read_file


async def test_make_distill_fn_binds_model_and_llm() -> None:
    llm = FakeLLM([LLMResponse(content="tiny")] * 5)
    distiller = make_distill_fn(llm, model="m", target_chars=100, bucket_chars=200)
    out = await distiller("x" * 1000, focus="f")
    assert len(out) <= 100 + 200


async def test_read_file_auto_distills_when_over_threshold(
    tmp_path: Path, fake_matrix_client
) -> None:
    """Big kb file → read_file returns a distilled version, not the raw 67KB."""
    big = "discord interactions\n" * 4000  # ~80 KB
    (tmp_path / "kb.md").write_text(big, encoding="utf-8")

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    async def fake_distill(text: str, focus: str) -> str:
        return f"[DISTILLED focus={focus} from {len(text)} chars] key facts: X, Y"

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        distill_fn=fake_distill,
        read_distill_threshold=1000,
        task_focus="design the command list",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["read_file"]({"path": "kb.md"})
    assert result.startswith("[DISTILLED")
    assert "design the command list" in result


async def test_read_file_skips_distillation_when_under_threshold(
    tmp_path: Path, fake_matrix_client
) -> None:
    small = "quick note\n"
    # Write exact bytes (byte-IO discipline): read_file now reads newline='' so a
    # text-mode CRLF from write_text on Windows would surface as \r\n.
    (tmp_path / "tiny.md").write_bytes(small.encode("utf-8"))

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    called = []

    async def fake_distill(text: str, focus: str) -> str:
        called.append((len(text), focus))
        return "should not be used"

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        distill_fn=fake_distill,
        read_distill_threshold=1000,
        task_focus="any",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["read_file"]({"path": "tiny.md"})
    assert result == small
    assert called == []


async def test_read_file_skips_distillation_without_focus(
    tmp_path: Path, fake_matrix_client
) -> None:
    """With no task_focus we have nothing to distill AGAINST — pass through as-is."""
    big = "x" * 5000
    (tmp_path / "large.md").write_text(big, encoding="utf-8")

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    called = []

    async def fake_distill(text: str, focus: str) -> str:
        called.append(focus)
        return "distilled"

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        distill_fn=fake_distill,
        read_distill_threshold=1000,
        task_focus="",  # intentionally empty
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["read_file"]({"path": "large.md"})
    assert result == big
    assert called == []


async def test_read_file_falls_back_to_truncation_when_distiller_raises(
    tmp_path: Path, fake_matrix_client
) -> None:
    big = "x" * 20_000
    (tmp_path / "large.md").write_text(big, encoding="utf-8")

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    async def broken_distill(text: str, focus: str) -> str:
        raise RuntimeError("distiller imploded")

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        distill_fn=broken_distill,
        read_distill_threshold=1000,
        task_focus="something",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["read_file"]({"path": "large.md"})
    # Fallback head-truncation kicks in — caller gets something usable.
    assert len(result) <= 1100
    assert "truncated" in result.lower()
