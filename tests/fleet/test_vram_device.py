"""Two-GPU device-resolution tests (June 30 wrong-device false-rejection).

Covers `_resolve_target_device`, device-pinned vs summed `probe_free_vram_mib`,
and the `check_model_fits` integration that queries the device Ollama uses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from agora.fleet import vram
from tests.conftest import TEST_OLLAMA_URL


def _nvidia_proc(stdout: bytes):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = 0
    return proc


class _PsResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _PsSession:
    def __init__(self, response):
        self._response = response

    def get(self, *_a, **_k):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


def _patch_ps(monkeypatch, payload, status=200):
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda **_k: _PsSession(_PsResponse(payload, status))
    )


# ------------------------------------------------------------- _resolve_target_device


async def test_resolve_target_device_none_when_daemon_unreachable(monkeypatch) -> None:
    class _Boom:
        def __init__(self, *_a, **_k): ...
        def get(self, *_a, **_k):
            raise vram.aiohttp.ClientError()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _Boom())
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    assert await vram._resolve_target_device("http://x:11434", "qwen2.5:7b") is None


async def test_resolve_target_device_parses_api_ps_device(monkeypatch) -> None:
    _patch_ps(monkeypatch, {"models": [{"name": "qwen2.5:7b-instruct", "gpu": 0}]})
    assert await vram._resolve_target_device("http://x:11434", "anything") == 0


async def test_resolve_target_device_parses_devices_list(monkeypatch) -> None:
    _patch_ps(monkeypatch, {"models": [{"name": "m", "gpus": [1]}]})
    assert await vram._resolve_target_device("http://x:11434", "m") == 1


async def test_resolve_target_device_falls_back_to_cuda_visible_devices(monkeypatch) -> None:
    # /api/ps has no device field → CUDA_VISIBLE_DEVICES is consulted.
    _patch_ps(monkeypatch, {"models": [{"name": "m"}]})
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    assert await vram._resolve_target_device("http://x:11434", "m") == 1


def test_cuda_visible_devices_single_int() -> None:
    assert vram._device_from_cuda_visible_devices({"CUDA_VISIBLE_DEVICES": "1"}) == 1
    assert vram._device_from_cuda_visible_devices({"CUDA_VISIBLE_DEVICES": "0"}) == 0


def test_cuda_visible_devices_list_or_uuid_is_none() -> None:
    assert vram._device_from_cuda_visible_devices({"CUDA_VISIBLE_DEVICES": "0,1"}) is None
    assert vram._device_from_cuda_visible_devices({"CUDA_VISIBLE_DEVICES": "GPU-abcd"}) is None
    assert vram._device_from_cuda_visible_devices({}) is None


def test_ps_entry_ignores_bool_and_negative() -> None:
    # bool is an int subclass — must not be read as a device index.
    assert vram._device_index_from_ps_entry({"gpu": True}) is None
    assert vram._device_index_from_ps_entry({"device": -1}) is None
    assert vram._device_index_from_ps_entry({"gpu_index": 2}) == 2


# ------------------------------------------------------------- probe_free_vram_mib


async def test_probe_pinned_device_queries_that_device_only() -> None:
    # nvidia-smi -i 1 returns just device 1's row (20000); device 0 (5000) ignored.
    proc = _nvidia_proc(b"20000\n")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
        free = await vram.probe_free_vram_mib(device_index=1)
    assert free == 20000
    # The -i 1 selector was passed to nvidia-smi.
    called_args = spawn.call_args.args
    assert "-i" in called_args and "1" in called_args


async def test_probe_fallback_sums_not_mins() -> None:
    proc = _nvidia_proc(b"5000\n20000\n")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        free = await vram.probe_free_vram_mib()  # device unknown
    assert free == 25000  # sum, NOT min (5000)


# ------------------------------------------------------------- check_model_fits integration


async def test_check_model_fits_queries_ollamas_device(monkeypatch) -> None:
    """Daemon shows a model resident on device 0 → the (different) target model's
    fit math runs against device 0's VRAM, not device 1's."""
    # /api/ps: some model resident on device 0 (reveals Ollama uses device 0).
    monkeypatch.setattr(
        vram, "_device_from_ollama_ps", AsyncMock(return_value=0)
    )
    # Target model itself is NOT resident → proceed to the free-space probe.
    monkeypatch.setattr(vram, "_is_model_resident", AsyncMock(return_value=False))
    monkeypatch.setattr(vram, "get_model_size_mib", AsyncMock(return_value=5000))
    monkeypatch.setattr(vram, "_device_name", AsyncMock(return_value="Tesla P40"))

    seen: dict = {}

    async def _probe(device_index=None):
        seen["device_index"] = device_index
        return 20000 if device_index == 0 else 5000

    monkeypatch.setattr(vram, "probe_free_vram_mib", _probe)

    check = await vram.check_model_fits("ollama/qwen2.5:7b-instruct", TEST_OLLAMA_URL, safety_margin_mib=512)
    assert seen["device_index"] == 0
    assert check.fits is True
    assert check.free_mib == 20000
    assert "device 0 (Tesla P40)" in check.reason


async def test_check_model_fits_reason_names_summed_fallback(monkeypatch) -> None:
    monkeypatch.setattr(vram, "_resolve_target_device", AsyncMock(return_value=None))
    monkeypatch.setattr(vram, "_is_model_resident", AsyncMock(return_value=False))
    monkeypatch.setattr(vram, "get_model_size_mib", AsyncMock(return_value=5000))
    monkeypatch.setattr(vram, "probe_free_vram_mib", AsyncMock(return_value=25000))
    check = await vram.check_model_fits("ollama/qwen2.5:7b-instruct", TEST_OLLAMA_URL)
    assert "all visible devices (summed)" in check.reason
