"""Hierarchical map-reduce distillation for large files.

When an agent does ``read_file`` on a 67 KB knowledge-base page, the result
overflows the model's context and the task fails. This module shrinks such
content *before* it reaches the agent's message history:

  1. Split the source into buckets that fit comfortably in one LLM call.
  2. For each bucket, run a focused extraction prompt that keeps only the
     facts relevant to the current task.
  3. Concatenate the per-bucket extracts.
  4. If the concatenated result still exceeds the target size, recurse —
     treating the merged extracts as the new input.
  5. Cap recursion depth so a pathologically large source can't run forever.

The LLM used for distillation is the same model the agents use, so no extra
dependencies and the token budget is per-call, not per-source.
"""

from __future__ import annotations

import logging
from typing import Any

from agora.fleet.llm_adapter import LLMProtocol

logger = logging.getLogger(__name__)

DEFAULT_TARGET_CHARS = 6_000
"""Roughly the size that leaves room for the task prompt + tool schemas."""

DEFAULT_BUCKET_CHARS = 4_000
"""Per-bucket input size. Below num_ctx by a wide margin so one call is reliable."""

DEFAULT_MAX_ROUNDS = 4
"""Safety cap. Recursion that hasn't converged by round 4 fallback-truncates."""

_EXTRACT_SYSTEM = (
    "You are a terse extraction engine. You receive an excerpt of documentation "
    "or source material and a task focus. Your job is to output only the parts "
    "of the excerpt that matter for that focus. Remove chrome, examples, and "
    "off-topic prose. Use concise bullet points or tight paragraphs. Do not "
    "add commentary, introductions, or meta-notes. If nothing in the excerpt "
    "is relevant, output the single line: (no relevant content)."
)


async def distill(
    text: str,
    *,
    focus: str,
    llm: LLMProtocol,
    model: str,
    target_chars: int = DEFAULT_TARGET_CHARS,
    bucket_chars: int = DEFAULT_BUCKET_CHARS,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> str:
    """Recursively shrink ``text`` while preserving information relevant to ``focus``.

    Returns a string of length ``<= target_chars`` (approximately — the last
    LLM call may overshoot). On internal failure (LLM errors, empty output)
    falls back to head-truncation with a ``[...truncated...]`` marker so the
    caller always receives *something* usable.
    """
    if len(text) <= target_chars:
        return text

    current = text
    for round_idx in range(1, max_rounds + 1):
        buckets = _split_into_buckets(current, bucket_chars)
        logger.info(
            "distill: round=%d buckets=%d source_chars=%d focus=%r",
            round_idx, len(buckets), len(current), focus[:80],
        )
        extracts: list[str] = []
        for i, bucket in enumerate(buckets, start=1):
            extract = await _extract_bucket(
                bucket=bucket, focus=focus, llm=llm, model=model, bucket_index=i,
                bucket_count=len(buckets),
            )
            extract = extract.strip()
            if extract and extract != "(no relevant content)":
                extracts.append(extract)
        merged = "\n\n".join(extracts).strip()

        if not merged:
            # Every bucket said "no relevant content" or LLM gave up. Fall
            # back to head-truncation so the caller gets *something*.
            return _head_truncate(text, target_chars, reason="no-distill-output")

        if len(merged) <= target_chars:
            return _annotate(merged, original_len=len(text), rounds=round_idx)

        current = merged

    # Didn't converge — last-ditch truncation of the latest round's output.
    return _head_truncate(current, target_chars, reason="max-rounds")


async def _extract_bucket(
    *,
    bucket: str,
    focus: str,
    llm: LLMProtocol,
    model: str,
    bucket_index: int,
    bucket_count: int,
) -> str:
    user_msg = (
        f"Task focus: {focus.strip()}\n\n"
        f"Excerpt ({bucket_index}/{bucket_count}):\n{bucket}\n\n"
        "Output only what's relevant to the focus."
    )
    try:
        resp = await llm.complete(
            messages=[{"role": "user", "content": user_msg}],
            system=_EXTRACT_SYSTEM,
            model=model,
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("distill: bucket %d/%d LLM call failed: %s", bucket_index, bucket_count, exc)
        return ""
    return resp.content or ""


def _split_into_buckets(text: str, bucket_chars: int) -> list[str]:
    """Split ``text`` into chunks of ``<= bucket_chars`` on line boundaries.

    Keeps markdown-ish structure intact — we never split a line in half.
    """
    if bucket_chars <= 0:
        return [text]
    lines = text.splitlines(keepends=True)
    buckets: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line)
        if line_len >= bucket_chars:
            # A single very-long line: flush current bucket and split the line.
            if current:
                buckets.append("".join(current))
                current, current_len = [], 0
            for i in range(0, line_len, bucket_chars):
                buckets.append(line[i : i + bucket_chars])
            continue
        if current_len + line_len > bucket_chars and current:
            buckets.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += line_len
    if current:
        buckets.append("".join(current))
    return buckets or [text]


def _head_truncate(text: str, target_chars: int, *, reason: str) -> str:
    marker = f"\n\n[...truncated by distiller: {reason}, from {len(text)} to {target_chars} chars...]"
    cut = max(0, target_chars - len(marker))
    return text[:cut] + marker


def _annotate(text: str, *, original_len: int, rounds: int) -> str:
    header = (
        f"[distilled from {original_len} chars to {len(text)} chars "
        f"in {rounds} round(s)]\n\n"
    )
    return header + text


def make_distill_fn(
    llm: LLMProtocol,
    *,
    model: str,
    target_chars: int = DEFAULT_TARGET_CHARS,
    bucket_chars: int = DEFAULT_BUCKET_CHARS,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
):
    """Return an ``async (text, focus) -> str`` closure bound to an LLM + model."""

    async def _call(text: str, focus: str) -> str:
        return await distill(
            text,
            focus=focus,
            llm=llm,
            model=model,
            target_chars=target_chars,
            bucket_chars=bucket_chars,
            max_rounds=max_rounds,
        )

    return _call


__all__ = [
    "DEFAULT_BUCKET_CHARS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_TARGET_CHARS",
    "distill",
    "make_distill_fn",
]
