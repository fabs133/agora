"""Tests for the FastMCP server wiring. We only verify tool registration —
execution paths are covered by :mod:`tests.mcp.test_handlers`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agora.mcp.server import build_server


def test_build_server_registers_expected_tools() -> None:
    handlers = MagicMock()
    server = build_server(handlers, name="agora-test")

    # FastMCP stores tools on an internal manager; use the documented list_tools API.
    import asyncio

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}

    expected = {
        "spawn_agent",
        "assign_task",
        "run_project",
        "create_flow",
        "run_flow",
        "agent_status",
        "get_kanban",
        "export_report",
    }
    assert expected <= names


def test_build_server_exposes_tool_schemas() -> None:
    handlers = MagicMock()
    server = build_server(handlers)
    import asyncio

    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    # spawn_agent tool should expose its required params in the inputSchema.
    schema = by_name["spawn_agent"].inputSchema
    assert "name" in schema.get("properties", {})
    assert "role" in schema.get("properties", {})
