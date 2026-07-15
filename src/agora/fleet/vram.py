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
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp

from agora.core.errors import AgoraError

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 5.0
OLLAMA_SHOW_TIMEOUT_SECONDS = 10.0


def _strip_provider_prefix(model: str) -> str:
    """Return the Ollama-native model name (drop a leading ``ollama/``).

    ``ollama/qwen2.5:7b-instruct`` → ``qwen2.5:7b-instruct``; an already-bare
    name is returned unchanged. Called only from Ollama-specific code paths
    (``/api/show`` + ``/api/ps`` queries), so a foreign provider prefix
    (``openai/…``) is a programming error — assert against it rather than
    silently posting a name Ollama can't resolve (the June 30 ~10:00 /api/show
    404 was the prefixed string leaking through).
    """
    if model.startswith("ollama/"):
        return model[len("ollama/"):]
    assert "/" not in model, (
        f"_strip_provider_prefix received a non-ollama model {model!r}; "
        "this helper is only valid on Ollama-specific code paths"
    )
    return model


@dataclass(frozen=True)
class VRAMCheck:
    """Verdict of the pre-flight free-VRAM probe. ``fits`` is the go/no-go;
    ``free_mib`` is ``None`` when the probe could not read the device (in which
    case the caller decides whether to proceed), and ``reason`` is a
    human-readable explanation for the log/error path."""

    fits: bool
    free_mib: int | None
    required_mib: int
    reason: str


# ------------------------------------------------------------------ VRAM probes


async def probe_free_vram_mib(device_index: int | None = None) -> int | None:
    """Return free VRAM in MiB, or ``None`` if no probe worked.

    ``device_index`` (when set) restricts the query to that single GPU — used
    when :func:`_resolve_target_device` has identified the device Ollama is
    using. When ``None`` (device unknown), the fallback **sums** free VRAM
    across all visible devices rather than taking the ``min()``: "all visible
    devices, summed" is a less-wrong default than "the device with the least
    free", which on a two-GPU box reports the wrong card whenever the other
    one is fuller (the June 30 09:58 false-rejection).
    """
    for probe in (_probe_nvidia_smi, _probe_rocm_smi):
        try:
            result = await probe(device_index)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s failed: %s", probe.__name__, exc)
            continue
        if result is not None:
            return result
    return None


async def _probe_nvidia_smi(device_index: int | None = None) -> int | None:
    args = ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"]
    if device_index is not None:
        args.insert(1, str(device_index))
        args.insert(1, "-i")
    stdout = await _run_cmd(*args)
    if stdout is None:
        return None
    values = [int(line.strip()) for line in stdout.splitlines() if line.strip().isdigit()]
    if not values:
        return None
    if device_index is not None:
        # Pinned to one device: -i returns exactly that device's row.
        return values[0]
    # Device unknown: sum across all visible devices (NOT min — see docstring).
    return sum(values)


async def _probe_rocm_smi(device_index: int | None = None) -> int | None:
    # device_index selection is nvidia-specific in v1; rocm always aggregates.
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
    return sum(free_values) if free_values else None


async def _device_name(device_index: int) -> str | None:
    """Best-effort GPU product name for ``device_index`` (e.g. ``"Tesla P40"``)."""
    stdout = await _run_cmd(
        "nvidia-smi", "-i", str(device_index),
        "--query-gpu=name", "--format=csv,noheader",
    )
    if not stdout:
        return None
    name = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    return name or None


def _device_from_cuda_visible_devices(env: Mapping[str, str] | None = None) -> int | None:
    """Parse a single GPU index from ``CUDA_VISIBLE_DEVICES`` in ``env``.

    Only the simple single-integer case is honoured (``"1"`` → 1). A comma list
    (``"0,1"``) or a GPU-UUID value can't be mapped to one nvidia-smi index, so
    those return ``None`` (→ the sum fallback). ``CUDA_VISIBLE_DEVICES`` is a
    CUDA-runtime remap whose value IS the physical index nvidia-smi addresses.
    """
    src = env if env is not None else os.environ
    raw = (src.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if raw.isdigit():
        return int(raw)
    return None


async def _device_from_ollama_ps(base_url: str) -> int | None:
    """Return the GPU index Ollama is using, parsed from ``GET /api/ps``.

    Inspects resident model entries for a device index. The exact field Ollama
    exposes has shifted between versions and is not yet verified against a live
    0.24 daemon, so this is defensive: it checks the candidate keys an entry
    might carry and returns the first non-negative int found, else ``None`` (→
    the next resolution signal). Any resident model reveals Ollama's device —
    a fresh load lands on the same card.
    """
    url = f"{base_url.rstrip('/')}/api/ps"
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS, sock_connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except (TimeoutError, aiohttp.ClientError):
        return None
    for entry in data.get("models") or []:
        idx = _device_index_from_ps_entry(entry)
        if idx is not None:
            return idx
    return None


def _device_index_from_ps_entry(entry: dict) -> int | None:
    """Extract a non-negative GPU index from one /api/ps model entry, if present.

    Checks candidate locations (a top-level int, or the first element of a
    ``gpus``/``devices`` list of ints). Unverified against live 0.24 — returns
    ``None`` when no recognised device field is present.
    """
    for key in ("gpu_index", "device", "gpu"):
        val = entry.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return val
    for key in ("gpus", "devices"):
        seq = entry.get(key)
        if isinstance(seq, list):
            for val in seq:
                if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                    return val
    return None


async def _resolve_target_device(base_url: str, model: str | None) -> int | None:
    """Resolve which GPU index Ollama is / will be using, or ``None`` if unknown.

    Resolution order (v1 implements 1, 4, 5; the daemon-env and log-parse
    options are deferred):

    1. Live ``GET /api/ps`` device parse (see :func:`_device_from_ollama_ps`).
    4. ``CUDA_VISIBLE_DEVICES`` from this process's own env.
    5. ``None`` → caller sums free VRAM across all visible devices.
    """
    idx = await _device_from_ollama_ps(base_url)
    if idx is not None:
        return idx
    return _device_from_cuda_visible_devices()


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
    effective = _strip_provider_prefix(model)
    url = f"{base_url.rstrip('/')}/api/show"
    timeout = aiohttp.ClientTimeout(total=OLLAMA_SHOW_TIMEOUT_SECONDS, sock_connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"name": effective}) as resp:
                if resp.status != 200:
                    return estimate_model_size_mib(effective)
                data = await resp.json()
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.debug("ollama /api/show failed for %s: %s", effective, exc)
        return estimate_model_size_mib(effective)

    size_bytes = data.get("size") or 0
    if isinstance(size_bytes, int) and size_bytes > 0:
        # Loaded VRAM ≈ weights + ~20% context/kv cache budget.
        return int(size_bytes / (1024 * 1024) * 1.2)
    return estimate_model_size_mib(effective)


# ---------------------------------------------------------------- Fit check


async def check_model_fits(
    model: str,
    base_url: str,  # required config-shaped endpoint — no localhost default; inject from Settings.ollama_base_url
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
    effective = _strip_provider_prefix(model)

    # If Ollama reports the target model as currently loaded, it fits by definition.
    if await _is_model_resident(effective, base_url):
        return VRAMCheck(
            fits=True,
            free_mib=None,
            required_mib=0,
            reason=f"{effective} already resident in VRAM (skipping free-space check)",
        )

    required = await get_model_size_mib(model, base_url)

    # Probe the device Ollama is actually using, not min() across all cards.
    device_index = await _resolve_target_device(base_url, effective)
    free = await probe_free_vram_mib(device_index=device_index)
    device_desc = await _describe_device(device_index)

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
            reason=(
                f"{required} MiB needed, {free} MiB free on {device_desc} "
                f"(margin {safety_margin_mib} MiB)"
            ),
        )
    return VRAMCheck(
        fits=False,
        free_mib=free,
        required_mib=required,
        reason=(
            f"model {model} needs ~{required} MiB VRAM, only {free} MiB free on "
            f"{device_desc} (reserve {safety_margin_mib} MiB); pick a smaller model "
            f"or evict other models first (POST /api/generate with keep_alive=0)"
        ),
    )


async def _describe_device(device_index: int | None) -> str:
    """Human-readable description of the device the math ran against."""
    if device_index is None:
        return "all visible devices (summed)"
    name = await _device_name(device_index)
    return f"device {device_index} ({name})" if name else f"device {device_index}"


async def _is_model_resident(model: str, base_url: str) -> bool:
    """True iff ``model`` appears in Ollama's /api/ps (currently loaded list)."""
    model = _strip_provider_prefix(model)  # defense in depth; idempotent
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
    base_url: str,  # required config-shaped endpoint — no localhost default; inject from Settings.ollama_base_url
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
