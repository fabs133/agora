"""Tests for the HTTPS fetcher with SSRF guard + trafilatura extraction."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agora.fleet.web_fetch import (
    FetchCache,
    FetchError,
    SSRFError,
    _extract_text,
    safe_fetch,
    validate_url,
)


# ================================================================ URL validation


def test_rejects_http_scheme() -> None:
    with pytest.raises(SSRFError, match="only https"):
        validate_url("http://docs.discord.com/")


def test_rejects_file_scheme() -> None:
    with pytest.raises(SSRFError, match="only https"):
        validate_url("file:///etc/passwd")


def test_rejects_data_scheme() -> None:
    with pytest.raises(SSRFError, match="only https"):
        validate_url("data:text/plain,hello")


def test_rejects_empty_url() -> None:
    with pytest.raises(SSRFError, match="empty"):
        validate_url("")


def test_rejects_url_without_hostname() -> None:
    with pytest.raises(SSRFError):
        validate_url("https:///path")


def test_rejects_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(SSRFError, match="non-public IP"):
        validate_url("https://localhost/")


def test_rejects_rfc1918(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(SSRFError, match="non-public IP"):
        validate_url("https://internal.example.com/")


def test_rejects_link_local(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("169.254.1.1", 0))],
    )
    with pytest.raises(SSRFError, match="non-public IP"):
        validate_url("https://linklocal.example.com/")


def test_rejects_cloud_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("169.254.169.254", 0))],
    )
    with pytest.raises(SSRFError, match="metadata address"):
        validate_url("https://169.254.169.254/")


def test_rejects_ipv6_loopback(monkeypatch) -> None:
    import socket

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET6, 1, 6, "", ("::1", 0, 0, 0))],
    )
    with pytest.raises(SSRFError, match="non-public IP"):
        validate_url("https://localhost/")


def test_accepts_public_ipv4(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("140.82.112.3", 0))],
    )
    host, port = validate_url("https://github.com/foo")
    assert host == "github.com"
    assert port == 443


def test_accepts_custom_port(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("140.82.112.3", 0))],
    )
    _, port = validate_url("https://github.com:8443/")
    assert port == 8443


# ============================================================ Text extraction


_SAMPLE_HTML = """\
<!doctype html>
<html><head><title>Docs</title></head>
<body>
  <nav>menu stuff</nav>
  <main>
    <h1>discord.py 2.x: app_commands</h1>
    <p>Use <code>@bot.tree.command</code> to declare slash commands.</p>
    <p>Register them with <code>await bot.tree.sync()</code>.</p>
  </main>
  <footer>copyright</footer>
</body></html>
"""


def test_extract_text_keeps_main_content() -> None:
    out = _extract_text(_SAMPLE_HTML, url="https://discordpy.readthedocs.io/intro.html")
    # trafilatura may strip the <h1>; what matters is the body code examples.
    assert "bot.tree.command" in out
    assert "bot.tree.sync" in out
    # Nav and footer should be gone.
    assert "menu stuff" not in out
    assert "copyright" not in out


def test_extract_text_falls_back_when_trafilatura_fails() -> None:
    # Trafilatura returns None for empty/trivial pages; our fallback strips tags.
    out = _extract_text("<html><body><p>Hello</p></body></html>", url="https://x.example/")
    assert "Hello" in out


# ================================================================ safe_fetch


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        url: str = "https://docs.example.com/",
    ) -> None:
        self.status = status
        self._body = body
        self.url = _FakeURL(url)
        self.content = _FakeContent(body)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None


class _FakeURL:
    def __init__(self, url: str) -> None:
        from urllib.parse import urlparse

        self.host = urlparse(url).hostname


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def get(self, *_a: object, **_k: object) -> _FakeResponse:
        return self._response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.fixture
def public_dns(monkeypatch):
    """Make all hostname resolutions return a public-IP stub."""
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("140.82.112.3", 0))],
    )


async def test_safe_fetch_happy_path(public_dns, monkeypatch) -> None:
    response = _FakeResponse(
        status=200,
        body=_SAMPLE_HTML.encode("utf-8"),
        url="https://docs.example.com/page",
    )
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda **_k: _FakeSession(response)
    )
    text = await safe_fetch("https://docs.example.com/page")
    assert "bot.tree.command" in text


async def test_safe_fetch_rejects_http(public_dns) -> None:
    with pytest.raises(SSRFError):
        await safe_fetch("http://docs.example.com/")


async def test_safe_fetch_raises_on_http_error(public_dns, monkeypatch) -> None:
    response = _FakeResponse(status=404, body=b"not found")
    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _FakeSession(response))
    with pytest.raises(FetchError, match="HTTP 404"):
        await safe_fetch("https://docs.example.com/gone")


async def test_safe_fetch_uses_cache(public_dns, monkeypatch) -> None:
    cache = FetchCache()
    cache.put("https://docs.example.com/page", "precomputed content")
    calls = []

    def _boom(**_k):
        calls.append(True)
        raise AssertionError("network should not be called on cache hit")

    monkeypatch.setattr("aiohttp.ClientSession", _boom)
    text = await safe_fetch("https://docs.example.com/page", cache=cache)
    assert text == "precomputed content"
    assert calls == []


async def test_safe_fetch_populates_cache(public_dns, monkeypatch) -> None:
    response = _FakeResponse(
        status=200, body=_SAMPLE_HTML.encode("utf-8"), url="https://docs.example.com/page"
    )
    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _FakeSession(response))
    cache = FetchCache()
    await safe_fetch("https://docs.example.com/page", cache=cache)
    assert cache.get("https://docs.example.com/page") is not None


async def test_safe_fetch_truncates_large_text(public_dns, monkeypatch) -> None:
    big = ("<p>" + ("x" * 500) + "</p>") * 200
    html = f"<html><body><main>{big}</main></body></html>"
    response = _FakeResponse(status=200, body=html.encode("utf-8"), url="https://x.example/")
    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _FakeSession(response))
    text = await safe_fetch("https://x.example/", max_text_bytes=1024)
    assert len(text.encode("utf-8")) <= 1024 + 200  # cap + truncation marker
    assert "truncated by agora" in text


async def test_safe_fetch_timeout_surfaces_as_fetch_error(public_dns, monkeypatch) -> None:
    class _HangingSession:
        def __init__(self, *_a, **_k): ...
        def get(self, *_a, **_k):
            raise asyncio.TimeoutError()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _HangingSession)
    # Skip the real backoff sleep so the retry path doesn't slow the test.
    monkeypatch.setattr("agora.fleet.web_fetch.RETRY_BACKOFF_SECONDS", 0.0)
    with pytest.raises(FetchError, match="timeout"):
        await safe_fetch("https://x.example/", timeout_seconds=0.1)


# -------------------------------------------------------------- retry semantics


async def test_safe_fetch_retries_once_on_timeout_then_succeeds(
    public_dns, monkeypatch
) -> None:
    """First attempt times out, second succeeds → overall returns the content."""
    monkeypatch.setattr("agora.fleet.web_fetch.RETRY_BACKOFF_SECONDS", 0.0)

    calls = {"n": 0}
    ok_response = _FakeResponse(
        status=200, body=_SAMPLE_HTML.encode("utf-8"), url="https://x.example/"
    )

    class _FlakySession:
        def __init__(self, *_a, **_k): ...
        def get(self, *_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncio.TimeoutError()
            return ok_response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FlakySession)
    text = await safe_fetch("https://x.example/")
    assert "bot.tree.command" in text
    assert calls["n"] == 2


async def test_safe_fetch_retries_once_on_5xx_then_succeeds(
    public_dns, monkeypatch
) -> None:
    monkeypatch.setattr("agora.fleet.web_fetch.RETRY_BACKOFF_SECONDS", 0.0)

    calls = {"n": 0}
    err_response = _FakeResponse(status=503, body=b"")
    ok_response = _FakeResponse(
        status=200, body=_SAMPLE_HTML.encode("utf-8"), url="https://x.example/"
    )

    class _FlakySession:
        def __init__(self, *_a, **_k): ...
        def get(self, *_a, **_k):
            calls["n"] += 1
            return ok_response if calls["n"] >= 2 else err_response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FlakySession)
    text = await safe_fetch("https://x.example/")
    assert "bot.tree.command" in text
    assert calls["n"] == 2


async def test_safe_fetch_does_not_retry_on_404(public_dns, monkeypatch) -> None:
    """4xx (except 429) must not retry — the URL is wrong, retry won't help."""
    monkeypatch.setattr("agora.fleet.web_fetch.RETRY_BACKOFF_SECONDS", 0.0)

    calls = {"n": 0}

    def _session(*_a, **_k):
        calls["n"] += 1
        return _FakeSession(_FakeResponse(status=404, body=b""))

    monkeypatch.setattr("aiohttp.ClientSession", _session)
    with pytest.raises(FetchError, match="HTTP 404"):
        await safe_fetch("https://x.example/gone")
    assert calls["n"] == 1


async def test_safe_fetch_does_not_retry_on_ssrf() -> None:
    """SSRF block is a policy decision — no point retrying."""
    with pytest.raises(SSRFError):
        await safe_fetch("http://not-https.example/")


async def test_safe_fetch_retries_on_429(public_dns, monkeypatch) -> None:
    monkeypatch.setattr("agora.fleet.web_fetch.RETRY_BACKOFF_SECONDS", 0.0)
    calls = {"n": 0}
    err_response = _FakeResponse(status=429, body=b"")
    ok_response = _FakeResponse(
        status=200, body=_SAMPLE_HTML.encode("utf-8"), url="https://x.example/"
    )

    class _FlakySession:
        def __init__(self, *_a, **_k): ...
        def get(self, *_a, **_k):
            calls["n"] += 1
            return ok_response if calls["n"] >= 2 else err_response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FlakySession)
    text = await safe_fetch("https://x.example/")
    assert "bot.tree.command" in text
    assert calls["n"] == 2


async def test_safe_fetch_retries_bounded_to_two_attempts(
    public_dns, monkeypatch
) -> None:
    """A permanently-timing-out endpoint must not retry forever."""
    monkeypatch.setattr("agora.fleet.web_fetch.RETRY_BACKOFF_SECONDS", 0.0)
    calls = {"n": 0}

    class _AlwaysTimeoutSession:
        def __init__(self, *_a, **_k): ...
        def get(self, *_a, **_k):
            calls["n"] += 1
            raise asyncio.TimeoutError()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _AlwaysTimeoutSession)
    with pytest.raises(FetchError, match="timeout"):
        await safe_fetch("https://x.example/")
    assert calls["n"] == 2


async def test_fetch_url_tool_dispatches_to_fetcher(tmp_path, fake_matrix_client) -> None:
    """The fetch_url inner tool executes the wired fetcher."""
    from agora.core.types import AgentRole
    from agora.fleet.inner_tools import ToolContext, get_tool_executor

    agent_room = await fake_matrix_client.create_room("agent")
    project_room = await fake_matrix_client.create_room("proj")

    received: list[str] = []

    async def _fake_fetch(url: str) -> str:
        received.append(url)
        return f"fetched({url})"

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        fetch_fn=_fake_fetch,
    )
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    result = await executor["fetch_url"]({"url": "https://discordpy.readthedocs.io/"})
    assert "fetched(https://discordpy.readthedocs.io/)" == result
    assert received == ["https://discordpy.readthedocs.io/"]


async def test_fetch_url_tool_reports_error_when_unwired(tmp_path, fake_matrix_client) -> None:
    from agora.core.types import AgentRole
    from agora.fleet.inner_tools import ToolContext, get_tool_executor

    agent_room = await fake_matrix_client.create_room("agent")
    project_room = await fake_matrix_client.create_room("proj")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
    )
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    result = await executor["fetch_url"]({"url": "https://x.example/"})
    assert "not enabled" in result
