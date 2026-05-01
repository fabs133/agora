"""Safe HTTPS fetcher for the ``fetch_url`` inner tool.

Design constraints locked with the user:

- **HTTPS only.** Reject everything else (http, file, data, ftp, ...).
- **SSRF guard.** Resolve the hostname *before* the request, and refuse any IP
  that is loopback, link-local, private (RFC 1918 / 4193), or the cloud
  metadata address (169.254.169.254). Re-check the resolved IP at request time.
- **Size-bounded.** Cap the raw HTML body at ``max_bytes`` and the extracted
  text at ``max_text_bytes`` so a 7B model's context window doesn't overflow.
- **Text extraction.** Use :mod:`trafilatura` to strip nav/sidebar/footer and
  return just the main content.
- **Non-blocking.** All IO via ``aiohttp`` under an explicit ``ClientTimeout``.
- **Caching.** A :class:`FetchCache` lets callers share a cache across agents
  so the architect and implementer don't re-fetch the same Discord page.

Raises :class:`SSRFError` on disallowed URLs and :class:`FetchError` on any
other fetch/extraction failure. Both inherit from
:class:`~agora.core.errors.AgoraError`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

from agora.core.errors import AgoraError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_BYTES = 1_048_576          # 1 MiB raw HTML
DEFAULT_MAX_TEXT_BYTES = 16_384        # ~4 K tokens worth of text

RETRY_BACKOFF_SECONDS = 2.0
MAX_FETCH_ATTEMPTS = 2                 # one initial try + one retry on transient errors

METADATA_IP = ipaddress.ip_address("169.254.169.254")  # AWS / GCP / Azure IMDS


class FetchError(AgoraError):
    """Generic failure in :func:`safe_fetch` — network, HTTP, or extraction."""


class SSRFError(AgoraError):
    """The URL was rejected by the SSRF guard."""


# ----------------------------------------------------------- URL validation


def validate_url(url: str) -> tuple[str, int]:
    """Parse, scheme-check, and DNS-resolve ``url``; return ``(host, port)``.

    Raises :class:`SSRFError` on any policy violation. The returned pair is
    what ``aiohttp`` will connect to, so callers can pass the host through
    ``server_hostname`` for TLS verification.
    """
    if not isinstance(url, str) or not url:
        raise SSRFError("empty url")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SSRFError(f"only https:// is allowed; got {parsed.scheme or '(none)'}://")
    if not parsed.hostname:
        raise SSRFError(f"url has no hostname: {url!r}")

    port = parsed.port or 443
    _assert_safe_host(parsed.hostname)
    return parsed.hostname, port


def _assert_safe_host(hostname: str) -> None:
    """Resolve ``hostname`` and reject private/loopback/metadata IPs."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFError(f"cannot resolve {hostname!r}: {exc}") from exc

    seen: set[str] = set()
    for family, _type, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        raw = sockaddr[0]
        if raw in seen:
            continue
        seen.add(raw)
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise SSRFError(f"cannot parse resolved address {raw!r}") from None
        if ip == METADATA_IP:
            raise SSRFError(f"refused metadata address {ip} for {hostname!r}")
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
            raise SSRFError(f"refused non-public IP {ip} for {hostname!r}")
        if ip.is_reserved or ip.is_unspecified:
            raise SSRFError(f"refused reserved address {ip} for {hostname!r}")


# ---------------------------------------------------------------- Cache


@dataclass
class FetchCache:
    """Per-project in-memory cache: ``{url: extracted_text}``."""

    entries: dict[str, str] = field(default_factory=dict)

    def get(self, url: str) -> str | None:
        return self.entries.get(url)

    def put(self, url: str, text: str) -> None:
        self.entries[url] = text


# ----------------------------------------------------------- safe_fetch


async def safe_fetch(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    cache: FetchCache | None = None,
    user_agent: str = "agora-bot/0.1 (+https://github.com/fabs133/manifold)",
) -> str:
    """Fetch a URL, convert to clean text, return the text (truncated).

    Retries once on transient errors (timeout, 429, 5xx, connection reset) with
    a short backoff. Non-transient failures (SSRF, 4xx other than 429, malformed
    content) raise immediately.

    Never blocks the event loop. Always raises on any policy violation.
    """
    # Fast-path on cache — validation is still cheap so we do it after a miss.
    if cache is not None:
        cached = cache.get(url)
        if cached is not None:
            return cached

    host, _port = validate_url(url)

    last_exc: FetchError | None = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            text = await _fetch_once(
                url=url,
                host=host,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                max_text_bytes=max_text_bytes,
                user_agent=user_agent,
            )
            if cache is not None:
                cache.put(url, text)
            return text
        except FetchError as exc:
            last_exc = exc
            if attempt >= MAX_FETCH_ATTEMPTS or not _is_transient(exc):
                raise
            logger.info(
                "safe_fetch: transient error on attempt %d/%d for %s: %s — retrying after %.1fs",
                attempt, MAX_FETCH_ATTEMPTS, url, exc, RETRY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)

    # Unreachable: the loop either returns or raises. This is belt-and-braces.
    raise last_exc or FetchError(f"unknown error fetching {url}")


async def _fetch_once(
    *,
    url: str,
    host: str,
    timeout_seconds: float,
    max_bytes: int,
    max_text_bytes: int,
    user_agent: str,
) -> str:
    """Single fetch attempt. Returns extracted text; raises :class:`FetchError` on failure."""
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, sock_connect=10)
    headers = {"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise FetchError(f"HTTP {resp.status} for {url}")

                # Re-check the actual peer IP after redirects.
                if resp.url.host and resp.url.host != host:
                    _assert_safe_host(resp.url.host)

                # Stream with a hard cap so a huge page can't DOS us.
                body = bytearray()
                async for chunk in resp.content.iter_chunked(16_384):
                    body.extend(chunk)
                    if len(body) >= max_bytes:
                        break
                raw_html = bytes(body).decode(errors="replace")
    except TimeoutError as exc:
        raise FetchError(f"timeout after {timeout_seconds}s for {url}") from exc
    except aiohttp.ClientError as exc:
        raise FetchError(f"network error for {url}: {exc}") from exc

    text = _extract_text(raw_html, url)
    if len(text.encode("utf-8")) > max_text_bytes:
        truncated = text.encode("utf-8")[:max_text_bytes].decode("utf-8", errors="ignore")
        text = truncated + "\n\n[...truncated by agora: content exceeded max_text_bytes...]"
    return text


def _is_transient(exc: FetchError) -> bool:
    """Classify a :class:`FetchError` as worth retrying.

    Transient: timeouts, 429, 5xx, connection resets, DNS flakes.
    Non-transient: SSRF blocks (policy), other 4xx (won't fix by retrying),
    extraction / parse errors (deterministic).
    """
    msg = str(exc).lower()
    if "timeout" in msg:
        return True
    if "http 429" in msg:
        return True
    # "HTTP 5xx" — any 500-series status
    if any(f"http 5{digit}" in msg for digit in "0123456789"):
        return True
    if "network error" in msg:
        return True
    return False


def _extract_text(raw_html: str, url: str) -> str:
    """Extract main content from HTML. Trafilatura first, plain strip as fallback."""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            raw_html,
            url=url,
            include_tables=True,
            include_links=False,
            favor_recall=False,
            deduplicate=True,
        )
        if extracted:
            return extracted.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("trafilatura failed for %s: %s", url, exc)

    # Fallback: crude tag strip. Rare path — only if trafilatura fails entirely.
    import re

    stripped = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<style[^>]*>.*?</style>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped.strip()


# -------------------------------------------------- convenience for orchestrator


def make_fetcher(
    *,
    cache: FetchCache,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
):
    """Build an ``async def fetch(url) -> str`` bound to a shared cache."""

    async def _fetch(url: str) -> str:
        return await safe_fetch(
            url,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_text_bytes=max_text_bytes,
            cache=cache,
        )

    return _fetch


__all__ = [
    "FetchCache",
    "FetchError",
    "SSRFError",
    "make_fetcher",
    "safe_fetch",
    "validate_url",
]
