"""Tests for scripts/render_diagrams.py — the standalone PlantUML renderer.

Two levels: a pure encoding round-trip (always runs, no server) and one live
render of the committed exemplar (skipped when the PlantUML server is down).
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "render_diagrams", _REPO / "scripts" / "render_diagrams.py"
)
rd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rd)

_URLSAFE = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_=")


@pytest.mark.parametrize(
    "text",
    [
        "@startuml\nA -> B: hi\n@enduml\n",
        "@startuml tool_call_turn\nparticipant X\nX -> Y : émission — v7\n@enduml\n",  # non-ascii
        "@startuml\n" + "\n".join(f"n{i} -> n{i+1}" for i in range(200)) + "\n@enduml\n",  # large
    ],
)
def test_encode_decode_round_trip(text: str) -> None:
    """plantuml_encode is lossless: decode(encode(text)) == text, and the token
    stays within PlantUML's URL alphabet (base64 '=' padding included)."""
    token = rd.plantuml_encode(text)
    assert set(token) <= _URLSAFE
    assert rd.plantuml_decode(token) == text


def test_encode_is_deterministic() -> None:
    text = "@startuml\nA -> B\n@enduml\n"
    assert rd.plantuml_encode(text) == rd.plantuml_encode(text)


def test_live_render_tool_call_turn(tmp_path) -> None:
    """Live fixture-check: render the committed exemplar and assert the server
    returns a non-empty SVG. Skipped when the PlantUML server is unreachable so a
    serverless checkout / CI does not fail. Renders into a tmp copy so the test
    never rewrites the committed docs/architecture/tool_call_turn.svg."""
    if not rd.server_reachable():
        pytest.skip(f"PlantUML server unreachable at {rd.PLANTUML_URL}")
    src = _REPO / "docs" / "architecture" / "tool_call_turn.puml"
    assert src.exists(), "committed exemplar tool_call_turn.puml is missing"
    tmp_puml = tmp_path / "tool_call_turn.puml"
    shutil.copyfile(src, tmp_puml)

    out = rd.render(tmp_puml)

    assert out == tmp_puml.with_suffix(".svg")
    data = out.read_bytes()
    assert data, "rendered SVG is empty"
    assert b"<svg" in data[:200], f"output is not SVG: {data[:80]!r}"
