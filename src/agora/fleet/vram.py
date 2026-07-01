"""VRAM pre-flight check.

Probes the system's free GPU VRAM and the model's estimated memory footprint,
then decides whether the model will fit. Runs asynchronously via
``asyncio.create_subprocess_exec`` so it never blocks the event loop.

Failure modes (probe returns ``None``) are treated as "unknown" rather than
"fits" — callers log and skip unless the user has opted out of the check.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

from agora.core.errors import AgoraError

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 5.0
OLLAMA_SHOW_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class VRAMCheck:
    fits: bool
    free_mib: int | None
    required_mib: int
    reason: str


# ------------------------------------------------------------------ VRAM probes


async def probe_free_vram_mib() -> int | None:
    """Return free VRAM in MiB, or ``None`` if no probe worked."""
    for probe in (_probe_nvidia_smi, _probe_rocm_smi):
        try:
            result = await probe()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s failed: %s", probe.__name__, exc)
            continue
        if result is not None:
            return result
    return None


async def _probe_nvidia_smi() -> int | None:
    stdout = await _run_cmd(
        "nvidia-smi",
        "--query-gpu=memory.free",
        "--format=csv,noheader,nounits",
    )
    if stdout is None:
        return None
    # Take the smallest free slot across GPUs (Ollama pins a single device).
    values = [int(line.strip()) for line in stdout.splitlines() if line.strip().isdigit()]
    return min(values) if values else None


async def _probe_rocm_smi() -> int | None:
    stdout = await _run_cmd("rocm-smi", "--showmeminfo", "vram", "--csv")
    if stdout is None:
        return None
    # rocm-smi CSV: GPU,vram Total,vram Used. Compute free = total - used.
    free_values: list[int] = []
    for line in stdout.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total = int(parts[1])
            used = int(parts[2])
        except ValueError:
            continue
        # rocm-smi reports bytes; convert to MiB.
        free_values.append((total - used) // (1024 * 1024))
    return min(free_values) if free_values else None


async def _run_cmd(*args: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace")


# ---------------------------------------------------------------- Model sizing

# Rough Q4_K_M quantization footprints for heuristic fallback (MiB).
_MODEL_SIZE_HEURISTIC = [
    (re.compile(r":?(70|72)b", re.I), 42_000),
    (re.compile(r":?(32|34)b", re.I), 20_000),
    (re.compile(r":?(22|24)b", re.I), 14_000),
    (re.compile(r":?1[34]b", re.I), 9_000),
    (re.compile(r":?(8|9)b", re.I), 6_000),
    (re.compile(r":?7b", re.I), 5_000),
    (re.compile(r":?3b", re.I), 2_500),
    (re.compile(r":?1(\.\d+)?b", re.I), 1_500),
]


def estimate_model_size_mib(model: str) -> int:
    """Heuristic model-size estimate when Ollama has no better answer."""
    for pattern, mib in _MODEL_SIZE_HEURISTIC:
        if pattern.search(model):
            return mib
    # Conservative default when we cannot recognise the name.
    return 8_000


async def get_model_size_mib(model: str, base_url: str) -> int:
    """Query Ollama for the model's on-disk size; fall back to the heuristic."""
    url = f"{base_url.rstrip('/')}/api/show"
    timeout = aiohttp.ClientTimeout(total=OLLAMA_SHOW_TIMEOUT_SECONDS, sock_connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"name": model}) as resp:
                if resp.status != 200:
                    return estimate_model_size_mib(model)
                data = await resp.json()
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.debug("ollama /api/show failed for %s: %s", model, exc)
        return estimate_model_size_mib(model)

    size_bytes = data.get("size") or 0
    if isinstance(size_bytes, int) and size_bytes > 0:
        # Loaded VRAM ≈ weights + ~20% context/kv cache budget.
        return int(size_bytes / (1024 * 1024) * 1.2)
    return estimate_model_size_mib(model)


# ---------------------------------------------------------------- Fit check


async def check_model_fits(
    model: str,
    base_url: str = "http://localhost:11434",
    safety_margin_mib: int = 512,
) -> VRAMCheck:
    """Decide whether ``model`` will fit in the system's free VRAM.

    ``fits=True`` when we can prove it fits. ``fits=True`` with
    ``free_mib=None`` when the probe failed (we do not block on unknown).
    ``fits=False`` only when we can prove it will not fit.

    Special case: if ``model`` is **already loaded** into VRAM (visible in
    ``/api/ps``), no fresh allocation is needed — we return ``fits=True``
    without demanding more free VRAM. This avoids a false negative when a
    previous run or warmup left the model resident.
    """
    effective = model.removeprefix("ollama/") or model

    # If Ollama reports the target model as currently loaded, it fits by definition.
    if await _is_model_resident(effective, base_url):
        return VRAMCheck(
            fits=True,
            free_mib=None,
            required_mib=0,
            reason=f"{effective} already resident in VRAM (skipping free-space check)",
        )

    required = await get_model_size_mib(model, base_url)
    free = await probe_free_vram_mib()

    if free is None:
        return VRAMCheck(
            fits=True,
            free_mib=None,
            required_mib=required,
            reason="GPU probe unavailable; assuming fit (set AGORA_SKIP_VRAM_CHECK=1 to silence)",
        )

    budget = free - safety_margin_mib
    if required <= budget:
        return VRAMCheck(
            fits=True,
            free_mib=free,
            required_mib=required,
            reason=f"{required} MiB needed, {free} MiB free (margin {safety_margin_mib} MiB)",
        )
    return VRAMCheck(
        fits=False,
        free_mib=free,
        required_mib=required,
        reason=(
            f"model {model} needs ~{required} MiB VRAM, only {free} MiB free "
            f"(reserve {safety_margin_mib} MiB); pick a smaller model or evict"
            f" other models first (POST /api/generate with keep_alive=0)"
        ),
    )


async def _is_model_resident(model: str, base_url: str) -> bool:
    """True iff ``model`` appears in Ollama's /api/ps (currently loaded list)."""
    url = f"{base_url.rstrip('/')}/api/ps"
    timeout = aiohttp.ClientTimeout(total=5, sock_connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
    except (TimeoutError, aiohttp.ClientError):
        return False
    for entry in data.get("models") or []:
        name = str(entry.get("name") or entry.get("model") or "")
        if name == model or name.startswith(f"{model}:"):
            return True
    return False


def raise_if_wont_fit(check: VRAMCheck, model: str) -> None:
    """Raise :class:`AgoraError` when a check proved the model will not fit."""
    if check.fits:
        return
    raise AgoraError(f"VRAM pre-flight failed: {check.reason}")


# ---------------------------------------------------------------- Warm-up


async def warmup(
    model: str,
    base_url: str = "http://localhost:11434",
    deadline_seconds: float = 600.0,
    keep_alive: str = "30m",
) -> None:
    """Load the model into VRAM before the real timeout clock starts.

    Sends a tiny ``/api/generate`` request with the supplied ``keep_alive``
    (default ``30m``) so the model stays resident for the duration of the
    run. The keep_alive value should match whatever the runtime adapter
    uses — otherwise the model can evict between warm-up and the first
    real turn. The timeout here is **independent** from
    ``llm_timeout_seconds`` — model loading can legitimately take minutes
    on cold disks, but we don't want that budget to eat the actual
    generation time on the first real turn.

    Raises :class:`AgoraError` on timeout or HTTP error so the orchestrator fails
    fast **before** creating identity rooms.
    """
    effective = model.removeprefix("ollama/") or model
    url = f"{base_url.rstrip('/')}/api/generate"
    payload = {
        "model": effective,
        "prompt": "hi",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": 1},
    }
    timeout = aiohttp.ClientTimeout(
        total=deadline_seconds,
        sock_connect=10,
        sock_read=deadline_seconds,
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise AgoraError(
                        f"Ollama warm-up HTTP {resp.status} for {effective!r}: {body[:200]}"
                    )
                await resp.json()
    except TimeoutError as exc:
        raise AgoraError(
            f"Ollama warm-up for {effective!r} timed out after {deadline_seconds}s"
        ) from exc
    except aiohttp.ClientError as exc:
        raise AgoraError(
            f"Ollama warm-up for {effective!r} failed: cannot reach {base_url}: {exc}"
        ) from exc
