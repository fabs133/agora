"""MCP tool handlers.

Each handler is a plain async callable that takes a dict of validated arguments
and returns a JSON-serializable dict. Handlers live separate from transport
(stdio / HTTP / SSE) so they can be unit-tested directly without running a real
MCP session.

The :class:`AgoraHandlers` class holds a running orchestrator plus a small
in-memory registry of spawned agents, projects, flows, and tasks for lookup by
id. State is intentionally ephemeral — the canonical source is the Matrix room
graph; this registry is a cache so the MCP caller can reference objects by id
without rehydrating on every call.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agora.core.agent import DEFAULT_MODEL, AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.errors import AgoraError
from agora.core.flow import Flow, load_flow
from agora.core.task import Task
from agora.core.types import AgentRole, ProjectPhase, TaskStatus
from agora.fleet.agent_runtime import TaskResult
from agora.fleet.orchestrator import Orchestrator, ProjectResult

logger = logging.getLogger(__name__)


@dataclass
class _AgentEntry:
    config: AgentConfig
    identity: AgentIdentity


@dataclass
class _ProjectEntry:
    id: str
    name: str
    phase: ProjectPhase
    agents: list[str]
    task_ids: list[str]
    started_at: str
    project_room_id: str | None = None
    result: ProjectResult | None = None
    task: asyncio.Task | None = None


@dataclass
class _TaskEntry:
    id: str
    description: str
    agent_id: str | None
    status: TaskStatus
    result: TaskResult | None = None
    created_at: str = ""


@dataclass
class _FlowEntry:
    name: str
    path: str
    flow: Flow


class AgoraHandlers:
    """Thin bridge between MCP tool calls and the :class:`Orchestrator`."""

    def __init__(self, orchestrator: Orchestrator, flows_dir: str | Path = "./flows") -> None:
        self._orch = orchestrator
        self._flows_dir = Path(flows_dir)
        self._agents: dict[str, _AgentEntry] = {}
        self._projects: dict[str, _ProjectEntry] = {}
        self._tasks: dict[str, _TaskEntry] = {}
        self._flows: dict[str, _FlowEntry] = {}

    # ----------------------------------------------------------------- spawn_agent

    async def spawn_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create an identity room for a new agent. Returns ``{agent_id, room_id}``."""
        config = _parse_agent_config(args)
        room_id, agent_id = await self._orch._rooms.create_identity_room(config)
        identity = AgentIdentity(agent_id=agent_id, room_id=room_id, config=config)
        self._agents[agent_id] = _AgentEntry(config=config, identity=identity)
        return {"agent_id": agent_id, "room_id": room_id, "name": config.name}

    # ----------------------------------------------------------------- assign_task

    async def assign_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Single-task mode: execute one task on an agent synchronously."""
        agent_name = _require_str(args, "agent_name")
        description = _require_str(args, "description")
        precondition_descriptions = list(args.get("preconditions", []) or [])
        postcondition_descriptions = list(args.get("postconditions", []) or [])

        entry = _find_agent(self._agents, agent_name)
        task = _make_task(
            description,
            agent_id=entry.config.name,
            precondition_descriptions=precondition_descriptions,
            postcondition_descriptions=postcondition_descriptions,
        )
        self._tasks[task.id] = _TaskEntry(
            id=task.id,
            description=description,
            agent_id=entry.config.name,
            status=TaskStatus.PENDING,
            created_at=_now(),
        )
        result = await self._orch.single_task(entry.config, task)
        self._tasks[task.id].status = TaskStatus.DONE if result.success else TaskStatus.FAILED
        self._tasks[task.id].result = result
        return {
            "task_id": task.id,
            "success": result.success,
            "output": result.output,
            "artifacts": result.artifacts,
            "postcondition_results": [
                {"name": n, "passed": p, "reason": r} for n, p, r in result.postcondition_results
            ],
        }

    # ----------------------------------------------------------------- run_project

    async def run_project(self, args: dict[str, Any]) -> dict[str, Any]:
        """Kick off a full project run. Runs async; poll via ``agent_status``."""
        name = _require_str(args, "name")
        agent_specs = args.get("agents") or []
        task_specs = args.get("tasks") or []
        if not agent_specs:
            raise AgoraError("run_project requires at least one agent")
        if not task_specs:
            raise AgoraError("run_project requires at least one task")

        agents = [_parse_agent_config(a) for a in agent_specs]
        tasks = [_parse_task_spec(t) for t in task_specs]

        project_id = str(uuid.uuid4())
        entry = _ProjectEntry(
            id=project_id,
            name=name,
            phase=ProjectPhase.INIT,
            agents=[a.name for a in agents],
            task_ids=[t.id for t in tasks],
            started_at=_now(),
        )
        self._projects[project_id] = entry

        # Seed task registry so kanban/status can report PENDING before execution.
        now = _now()
        for task in tasks:
            self._tasks[task.id] = _TaskEntry(
                id=task.id,
                description=task.description,
                agent_id=task.agent_id,
                status=TaskStatus.PENDING,
                created_at=now,
            )

        async def _run() -> None:
            try:
                entry.result = await self._orch.run_project(name, agents, tasks)
                entry.phase = entry.result.project.phase
                entry.project_room_id = entry.result.project_room_id
                by_id = {r.task_id: r for r in entry.result.task_results}
                for tid in entry.task_ids:
                    record = self._tasks.get(tid)
                    result = by_id.get(tid)
                    if record is None or result is None:
                        continue
                    record.status = TaskStatus.DONE if result.success else TaskStatus.FAILED
                    record.result = result
            except Exception as exc:  # noqa: BLE001
                logger.exception("project %s failed: %s", project_id, exc)
                entry.phase = ProjectPhase.FAILED

        entry.task = asyncio.create_task(_run())
        return {"project_id": project_id, "name": name, "phase": entry.phase.value}

    # ----------------------------------------------------------------- create_flow

    async def create_flow(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create and persist a :class:`Flow` from a spec dict, returning its id/path."""
        name = _require_str(args, "name")
        flow_dict = dict(args)
        flow_dict.setdefault("description", "")
        flow_dict.setdefault("agents", [])
        flow_dict.setdefault("task_graph", [])

        self._flows_dir.mkdir(parents=True, exist_ok=True)
        target = self._flows_dir / f"{name}.yaml"
        import yaml

        target.write_text(yaml.safe_dump(flow_dict, sort_keys=False), encoding="utf-8")
        flow = load_flow(target)
        self._flows[name] = _FlowEntry(name=name, path=str(target), flow=flow)
        return {"name": name, "path": str(target), "agents": [a.name for a in flow.agents]}

    async def run_flow(self, args: dict[str, Any]) -> dict[str, Any]:
        """Instantiate a saved flow and run it as a project."""
        flow_name = _require_str(args, "flow_name")
        project_name = args.get("project_name", flow_name)
        variables = args.get("variables") or {}
        entry = self._flows.get(flow_name)
        if entry is None:
            # Fallback: try to load from disk (e.g. one of the built-in flows).
            candidate = self._flows_dir / f"{flow_name}.yaml"
            if candidate.is_file():
                flow = load_flow(candidate)
                entry = _FlowEntry(name=flow_name, path=str(candidate), flow=flow)
                self._flows[flow_name] = entry
            else:
                raise AgoraError(f"unknown flow {flow_name!r} (did you call create_flow?)")

        from agora.core.flow import instantiate_flow

        agents, tasks = instantiate_flow(entry.flow, project_name, variables=variables)
        return await self.run_project(
            {
                "name": project_name,
                "agents": [_agent_config_to_dict(a) for a in agents],
                "tasks": [_task_to_spec(t) for t in tasks],
            }
        )

    async def list_flows(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        """List known flows — both the ones created this session and YAML files on disk."""
        on_disk: list[dict[str, Any]] = []
        if self._flows_dir.is_dir():
            for path in sorted(self._flows_dir.glob("*.yaml")):
                on_disk.append({"name": path.stem, "path": str(path)})
        return {
            "loaded": [
                {
                    "name": entry.name,
                    "path": entry.path,
                    "agents": [a.name for a in entry.flow.agents],
                    "task_count": len(entry.flow.task_graph),
                }
                for entry in self._flows.values()
            ],
            "on_disk": on_disk,
        }

    async def get_flow(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return a flow's resolved contents (agents, task_graph)."""
        name = _require_str(args, "flow_name")
        entry = self._flows.get(name)
        if entry is None:
            candidate = self._flows_dir / f"{name}.yaml"
            if not candidate.is_file():
                raise AgoraError(f"unknown flow {name!r}")
            flow = load_flow(candidate)
            entry = _FlowEntry(name=name, path=str(candidate), flow=flow)
            self._flows[name] = entry
        flow = entry.flow
        return {
            "name": flow.name,
            "description": flow.description,
            "agents": [_agent_config_to_dict(a) for a in flow.agents],
            "task_graph": [
                {
                    "id": t.id,
                    "assigned_to": t.assigned_to,
                    "description": t.description,
                    "depends_on": list(t.depends_on),
                    "preconditions": list(t.precondition_descriptions),
                    "postconditions": list(t.postcondition_descriptions),
                }
                for t in flow.task_graph
            ],
        }

    async def validate_flow(self, args: dict[str, Any]) -> dict[str, Any]:
        """Parse + validate a flow YAML snippet. Returns ``{valid, errors, summary}``."""
        yaml_text = _require_str(args, "yaml_text")
        tmp = self._flows_dir / f".validate-{uuid.uuid4().hex[:8]}.yaml"
        self._flows_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(yaml_text, encoding="utf-8")
        try:
            flow = load_flow(tmp)
        except AgoraError as exc:
            return {"valid": False, "errors": [str(exc)]}
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return {
            "valid": True,
            "errors": [],
            "summary": {
                "name": flow.name,
                "agents": [a.name for a in flow.agents],
                "task_count": len(flow.task_graph),
            },
        }

    # ----------------------------------------------------------------- agent_status

    async def agent_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Report status for an agent, a project, or everything (no args)."""
        agent_name = args.get("agent_name")
        project_id = args.get("project_id")

        if agent_name:
            entry = _find_agent(self._agents, agent_name)
            active = [
                t for t in self._tasks.values() if t.agent_id == entry.config.name
            ]
            return {
                "agent": {
                    "name": entry.config.name,
                    "role": entry.config.role.value,
                    "agent_id": entry.identity.agent_id,
                    "room_id": entry.identity.room_id,
                },
                "tasks": [_task_entry_to_dict(t) for t in active],
            }

        if project_id:
            proj = self._projects.get(project_id)
            if proj is None:
                raise AgoraError(f"unknown project_id {project_id!r}")
            return _project_entry_to_dict(proj, orchestrator=self._orch)

        return {
            "agents": [
                {
                    "name": e.config.name,
                    "role": e.config.role.value,
                    "agent_id": e.identity.agent_id,
                }
                for e in self._agents.values()
            ],
            "projects": [
                _project_entry_to_dict(p, orchestrator=self._orch)
                for p in self._projects.values()
            ],
            "tasks": [_task_entry_to_dict(t) for t in self._tasks.values()],
        }

    # ----------------------------------------------------------------- kanban + export

    async def get_kanban(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return a kanban snapshot derived from the handler registry.

        The pure observe.kanban.build_kanban function operates on Matrix events;
        for the in-memory registry we emit synthetic task events so the same
        fold applies and the two paths cannot diverge.
        """
        from agora.observe.kanban import build_kanban

        project_id = args.get("project_id")
        tasks = list(self._tasks.values())
        if project_id:
            proj = self._projects.get(project_id)
            if proj is None:
                raise AgoraError(f"unknown project_id {project_id!r}")
            tasks = [t for t in tasks if t.id in proj.task_ids]

        synthetic_events = [
            {
                "type": "m.agora.task",
                "content": {
                    "task_id": t.id,
                    "description": t.description,
                    "agent_id": t.agent_id,
                    "status": t.status.value,
                    "fingerprint": "",
                    "timestamp": t.created_at,
                },
            }
            for t in tasks
        ]
        board = build_kanban(synthetic_events)
        return {"project_id": project_id, "columns": board.to_dict()}

    async def export_report(self, args: dict[str, Any]) -> dict[str, Any]:
        """Write a standalone HTML report derived from handler state + synthetic events."""
        from agora.observe.export import ReportContext, write_report

        project_id = _require_str(args, "project_id")
        out_path = Path(args.get("path") or f"./report-{project_id}.html")
        proj = self._projects.get(project_id)
        if proj is None:
            raise AgoraError(f"unknown project_id {project_id!r}")

        # Build a synthetic event stream from the task registry so export.py can
        # use the same kanban/timeline fold it uses against real Matrix events.
        events: list[tuple[str, dict[str, Any]]] = []
        room_id = f"synthetic:{project_id}"
        for tid in proj.task_ids:
            t = self._tasks.get(tid)
            if t is None:
                continue
            events.append(
                (
                    room_id,
                    {
                        "type": "m.agora.task",
                        "content": {
                            "task_id": t.id,
                            "description": t.description,
                            "agent_id": t.agent_id,
                            "status": t.status.value,
                            "fingerprint": "",
                            "timestamp": t.created_at,
                        },
                    },
                )
            )
            if t.result is not None:
                events.append(
                    (
                        room_id,
                        {
                            "type": "m.agora.task_result",
                            "content": {
                                "task_id": t.id,
                                "success": t.result.success,
                                "output": t.result.output,
                                "artifacts": list(t.result.artifacts),
                                "postcondition_results": [
                                    {"name": n, "passed": p, "reason": r}
                                    for n, p, r in t.result.postcondition_results
                                ],
                                "timestamp": "",
                            },
                        },
                    )
                )

        context = ReportContext(
            project_name=proj.name,
            project_id=proj.id,
            phase=proj.phase.value,
            started_at=proj.started_at,
            ended_at=_now() if proj.result else "",
            total_tokens=proj.result.total_tokens if proj.result else {},
            duration_seconds=proj.result.duration_seconds if proj.result else 0.0,
            agents=list(proj.agents),
        )
        path = write_report(out_path, context, events)
        return {"project_id": project_id, "path": path}

    # ------------------------------------------------------------------ registry accessors (tests)

    @property
    def agents(self) -> dict[str, _AgentEntry]:
        return self._agents

    @property
    def projects(self) -> dict[str, _ProjectEntry]:
        return self._projects

    @property
    def tasks(self) -> dict[str, _TaskEntry]:
        return self._tasks

    @property
    def flows(self) -> dict[str, _FlowEntry]:
        return self._flows


# ================================ helpers ================================


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise AgoraError(f"missing or invalid required field '{key}'")
    return value


def _parse_agent_config(data: dict[str, Any]) -> AgentConfig:
    name = _require_str(data, "name")
    role_raw = _require_str(data, "role")
    try:
        role = AgentRole(role_raw)
    except ValueError as exc:
        raise AgoraError(f"invalid agent role {role_raw!r}") from exc
    return AgentConfig(
        name=name,
        role=role,
        model=data.get("model") or DEFAULT_MODEL,
        instructions=data.get("instructions", ""),
        knowledge_files=tuple(data.get("knowledge_files", []) or []),
    )


def _parse_task_spec(data: dict[str, Any]) -> Task:
    description = _require_str(data, "description")
    agent_id = data.get("agent_id")
    depends_on = tuple(data.get("depends_on", []) or [])
    return _make_task(
        description,
        agent_id=agent_id,
        task_id=data.get("id") or str(uuid.uuid4()),
        depends_on=depends_on,
        precondition_descriptions=list(data.get("preconditions", []) or []),
        postcondition_descriptions=list(data.get("postconditions", []) or []),
    )


def _make_task(
    description: str,
    *,
    agent_id: str | None,
    task_id: str | None = None,
    depends_on: tuple[str, ...] = (),
    precondition_descriptions: list[str] | None = None,
    postcondition_descriptions: list[str] | None = None,
) -> Task:
    preconds = precondition_descriptions or []
    postconds = postcondition_descriptions or []
    spec = Specification(
        preconditions=tuple(
            make_predicate(f"pre_{i}", desc, _always_true) for i, desc in enumerate(preconds)
        ),
        postconditions=tuple(
            make_predicate(f"post_{i}", desc, _always_true) for i, desc in enumerate(postconds)
        ),
        description=description,
    )
    now = _now()
    return Task(
        id=task_id or str(uuid.uuid4()),
        spec=spec,
        description=description,
        agent_id=agent_id,
        depends_on=depends_on,
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


def _always_true(_ctx: dict[str, Any]) -> tuple[bool, str]:
    return True, ""


def _find_agent(agents: dict[str, _AgentEntry], name: str) -> _AgentEntry:
    for entry in agents.values():
        if entry.config.name == name:
            return entry
    raise AgoraError(f"no agent named {name!r}")


def _project_entry_to_dict(
    entry: _ProjectEntry, orchestrator: Any | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": entry.id,
        "name": entry.name,
        "phase": entry.phase.value,
        "agents": list(entry.agents),
        "task_count": len(entry.task_ids),
        "started_at": entry.started_at,
        "done": entry.phase in (ProjectPhase.DONE, ProjectPhase.FAILED),
    }
    if orchestrator is not None and entry.project_room_id:
        control = orchestrator.get_control(entry.project_room_id)
        if control is not None:
            out["paused"] = not control.pause_event.is_set()
            out["aborted"] = control.is_aborted()
            if control.abort_reason:
                out["abort_reason"] = control.abort_reason
            if control.notes:
                out["pending_notes"] = list(control.notes)
            if control.agent_redirects:
                out["pending_redirects"] = dict(control.agent_redirects)
    return out


def _task_entry_to_dict(entry: _TaskEntry) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": entry.id,
        "description": entry.description,
        "status": entry.status.value,
        "agent_id": entry.agent_id,
    }
    if entry.result is not None:
        out["success"] = entry.result.success
        out["output"] = entry.result.output
        out["artifacts"] = list(entry.result.artifacts)
    return out


def _agent_config_to_dict(config: AgentConfig) -> dict[str, Any]:
    return {
        "name": config.name,
        "role": config.role.value,
        "model": config.model,
        "instructions": config.instructions,
        "knowledge_files": list(config.knowledge_files),
    }


def _task_to_spec(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "description": task.description,
        "agent_id": task.agent_id,
        "depends_on": list(task.depends_on),
        "preconditions": [p.description for p in task.spec.preconditions],
        "postconditions": [p.description for p in task.spec.postconditions],
    }
