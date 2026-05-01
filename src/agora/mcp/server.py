"""MCP server — wires :class:`AgoraHandlers` into FastMCP tool definitions.

Run with ``python -m agora.cli mcp`` or programmatically via :func:`build_server`.
Transport defaults to stdio; override via the ``transport`` arg.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from agora.mcp.handlers import AgoraHandlers


def build_server(handlers: AgoraHandlers, name: str = "agora") -> FastMCP:
    """Return a configured FastMCP server bound to ``handlers``.

    Tool definitions match :class:`AgoraHandlers` method signatures. Arguments are
    passed through as JSON strings to keep the tool surface simple; handlers
    parse and validate them.
    """
    mcp = FastMCP(name)

    @mcp.tool()
    async def spawn_agent(
        name: str,
        role: str,
        model: str | None = None,
        instructions: str = "",
        knowledge_files: list[str] | None = None,
    ) -> str:
        """Create a new agent identity room.

        Args:
            name: Agent display name. Must be unique.
            role: One of architect, implementer, reviewer, tester.
            model: LLM model identifier (defaults to claude-sonnet-4).
            instructions: Base system prompt for the agent.
            knowledge_files: Optional list of file paths to upload as knowledge.
        """
        result = await handlers.spawn_agent(
            {
                "name": name,
                "role": role,
                "model": model,
                "instructions": instructions,
                "knowledge_files": knowledge_files or [],
            }
        )
        return json.dumps(result)

    @mcp.tool()
    async def assign_task(
        agent_name: str,
        description: str,
        preconditions: list[str] | None = None,
        postconditions: list[str] | None = None,
    ) -> str:
        """Run a single task on an existing agent and return the result synchronously."""
        result = await handlers.assign_task(
            {
                "agent_name": agent_name,
                "description": description,
                "preconditions": preconditions or [],
                "postconditions": postconditions or [],
            }
        )
        return json.dumps(result)

    @mcp.tool()
    async def run_project(
        name: str,
        agents: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> str:
        """Kick off a full project run (async). Poll via ``agent_status(project_id=...)``."""
        result = await handlers.run_project(
            {"name": name, "agents": agents, "tasks": tasks}
        )
        return json.dumps(result)

    @mcp.tool()
    async def create_flow(
        name: str,
        description: str = "",
        agents: list[dict[str, Any]] | None = None,
        task_graph: list[dict[str, Any]] | None = None,
    ) -> str:
        """Persist a reusable flow (YAML) combining agents + a task graph template."""
        result = await handlers.create_flow(
            {
                "name": name,
                "description": description,
                "agents": agents or [],
                "task_graph": task_graph or [],
            }
        )
        return json.dumps(result)

    @mcp.tool()
    async def run_flow(flow_name: str, project_name: str | None = None) -> str:
        """Instantiate a saved flow and launch it as a project."""
        result = await handlers.run_flow(
            {"flow_name": flow_name, "project_name": project_name or flow_name}
        )
        return json.dumps(result)

    @mcp.tool()
    async def agent_status(
        agent_name: str | None = None, project_id: str | None = None
    ) -> str:
        """Report status for an agent, a project, or the full fleet (no args)."""
        result = await handlers.agent_status(
            {"agent_name": agent_name, "project_id": project_id}
        )
        return json.dumps(result)

    @mcp.tool()
    async def get_kanban(project_id: str | None = None) -> str:
        """Return a kanban snapshot of tasks grouped by status."""
        result = await handlers.get_kanban({"project_id": project_id})
        return json.dumps(result)

    @mcp.tool()
    async def export_report(project_id: str, path: str | None = None) -> str:
        """Write a markdown report of a project. Returns the output path."""
        result = await handlers.export_report({"project_id": project_id, "path": path})
        return json.dumps(result)

    return mcp


async def run_stdio(handlers: AgoraHandlers) -> None:
    """Run the server over stdio (the default MCP transport)."""
    server = build_server(handlers)
    await server.run_stdio_async()
