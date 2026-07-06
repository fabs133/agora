#!/usr/bin/env python
"""Render every ``docs/architecture/*.puml`` to a sibling ``.svg`` via the local
PlantUML server.

Standalone by design: stdlib only, no project imports, so it runs in a bare
checkout with nothing installed. It PlantUML-text-encodes each source (raw
DEFLATE + PlantUML's URL-safe base64 alphabet), GETs ``/svg/<encoded>`` from the
server, and writes the returned SVG next to the source.

The server is the ``plantuml-server`` docker container from
``D:\\Projekte\\mcp-server\\docker\\plantuml.yml`` (``ports: "18080:8080"``), so
the default URL is ``http://localhost:18080`` (override with ``AGORA_PLANTUML_URL``).
If it is unreachable the script exits LOUDLY rather than silently leaving stale
SVGs — a diagram that quietly failed to regenerate reads as "up to date" when it
is not.
"""

from __future__ import annotations

import base64
import os
import string
import urllib.error
import urllib.request
import zlib
from pathlib import Path

#: PlantUML server base URL. Port 18080 per mcp-server/docker/plantuml.yml.
PLANTUML_URL = os.environ.get("AGORA_PLANTUML_URL", "http://localhost:18080").rstrip("/")
#: Where the .puml sources live (rendered SVGs are written alongside them).
ARCH_DIR = Path(__file__).resolve().parents[1] / "docs" / "architecture"

# PlantUML uses standard base64 over the raw-deflated bytes, then remaps the
# alphabet to a URL-safe one. These translation tables convert between the two.
_B64 = (string.ascii_uppercase + string.ascii_lowercase + string.digits + "+/").encode()
_PLANTUML = (string.digits + string.ascii_uppercase + string.ascii_lowercase + "-_").encode()
_B64_TO_PLANTUML = bytes.maketrans(_B64, _PLANTUML)
_PLANTUML_TO_B64 = bytes.maketrans(_PLANTUML, _B64)


def plantuml_encode(text: str) -> str:
    """Encode PlantUML source into the server's URL token: raw-DEFLATE the UTF-8
    bytes (zlib stream minus its 2-byte header and 4-byte adler32 trailer), then
    base64 into PlantUML's ``0-9A-Za-z-_`` alphabet. Inverse of
    :func:`plantuml_decode`."""
    raw_deflate = zlib.compress(text.encode("utf-8"), 9)[2:-4]
    return base64.b64encode(raw_deflate).translate(_B64_TO_PLANTUML).decode("ascii")


def plantuml_decode(token: str) -> str:
    """Inverse of :func:`plantuml_encode` — used by the round-trip test to prove
    the encoding is lossless without needing the server."""
    raw_deflate = base64.b64decode(token.encode("ascii").translate(_PLANTUML_TO_B64))
    return zlib.decompress(raw_deflate, -15).decode("utf-8")


def server_reachable(url: str = PLANTUML_URL, timeout: float = 5.0) -> bool:
    """True iff the PlantUML server answers 200 at its root within ``timeout``."""
    try:
        with urllib.request.urlopen(url + "/", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def render(puml: Path, *, timeout: float = 30.0) -> Path:
    """Render one ``.puml`` file to a sibling ``.svg`` and return the SVG path."""
    token = plantuml_encode(puml.read_text(encoding="utf-8"))
    with urllib.request.urlopen(f"{PLANTUML_URL}/svg/{token}", timeout=timeout) as resp:
        svg_bytes = resp.read()
    out = puml.with_suffix(".svg")
    out.write_bytes(svg_bytes)
    return out


def main() -> int:
    if not server_reachable():
        raise SystemExit(
            f"[render_diagrams] PlantUML server unreachable at {PLANTUML_URL} — "
            "start the plantuml-server container (docker compose -f "
            "D:/Projekte/mcp-server/docker/plantuml.yml up -d) before rendering."
        )
    sources = sorted(ARCH_DIR.glob("*.puml"))
    if not sources:
        raise SystemExit(f"[render_diagrams] no *.puml sources under {ARCH_DIR}")
    for puml in sources:
        out = render(puml)
        print(f"rendered {puml.name} -> {out.name} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
