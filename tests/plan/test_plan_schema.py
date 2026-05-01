"""Pydantic schema coverage for v2.0 plan YAML.

Verifies the v2.0-only optional fields (``postconditions``, ``output_path``,
``stages``) load correctly AND that a v1.0 flow still validates without them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agora.core.errors import AgoraError
from agora.core.flow import (
    PostconditionRef,
    SUPPORTED_FLOW_VERSIONS,
    StageTemplate,
    load_flow,
    save_flow,
)


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_supported_versions_contains_both():
    assert "1.0" in SUPPORTED_FLOW_VERSIONS
    assert "2.0" in SUPPORTED_FLOW_VERSIONS


def test_v1_flow_still_loads(tmp_path: Path):
    _write_yaml(
        tmp_path / "v1.yaml",
        {
            "version": "1.0",
            "name": "minimal-v1",
            "description": "",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "t1",
                    "assigned_to": "a",
                    "description": "do the thing",
                    "postcondition_descriptions": ["artifact exists"],
                }
            ],
        },
    )
    flow = load_flow(tmp_path / "v1.yaml")
    assert len(flow.task_graph) == 1
    task = flow.task_graph[0]
    assert task.postconditions == ()  # v2.0 fields default to empty
    assert task.stages == ()
    assert task.output_path == ""
    assert task.postcondition_descriptions == ("artifact exists",)


def test_v2_flow_with_typed_postconditions(tmp_path: Path):
    _write_yaml(
        tmp_path / "v2.yaml",
        {
            "version": "2.0",
            "name": "minimal-v2",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "build",
                    "assigned_to": "a",
                    "description": "write bot.py",
                    "output_path": "bot.py",
                    "postconditions": [
                        {"name": "file_exists", "args": {"rel": "bot.py"}},
                        {"name": "mark_complete", "args": {}},
                    ],
                }
            ],
        },
    )
    flow = load_flow(tmp_path / "v2.yaml")
    task = flow.task_graph[0]
    assert task.output_path == "bot.py"
    assert len(task.postconditions) == 2
    first = task.postconditions[0]
    assert isinstance(first, PostconditionRef)
    assert first.name == "file_exists"
    assert first.args_dict() == {"rel": "bot.py"}


def test_v2_flow_with_stages(tmp_path: Path):
    _write_yaml(
        tmp_path / "v2.yaml",
        {
            "version": "2.0",
            "name": "staged",
            "agents": [{"name": "impl", "role": "implementer"}],
            "task_graph": [
                {
                    "id": "write",
                    "assigned_to": "impl",
                    "description": "stage it",
                    "output_path": "out.py",
                    "stages": [
                        {
                            "name": "write_skeleton",
                            "instruction": "Write exactly this content.",
                            "context_files": ["template.py"],
                            "max_iterations": 4,
                        },
                    ],
                }
            ],
        },
    )
    flow = load_flow(tmp_path / "v2.yaml")
    task = flow.task_graph[0]
    assert len(task.stages) == 1
    stage = task.stages[0]
    assert isinstance(stage, StageTemplate)
    assert stage.name == "write_skeleton"
    assert stage.context_files == ("template.py",)
    assert stage.max_iterations == 4


def test_v2_flow_rejects_unknown_version(tmp_path: Path):
    _write_yaml(
        tmp_path / "bad.yaml",
        {
            "version": "99.0",
            "name": "future",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [],
        },
    )
    with pytest.raises(AgoraError, match="unsupported flow version"):
        load_flow(tmp_path / "bad.yaml")


def test_save_flow_emits_v2_when_fields_present(tmp_path: Path):
    # Round-trip a v2.0 plan and verify save_flow picks the right version.
    src = tmp_path / "in.yaml"
    _write_yaml(
        src,
        {
            "version": "2.0",
            "name": "rt",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "x",
                    "assigned_to": "a",
                    "description": "do",
                    "output_path": "out.md",
                    "postconditions": [{"name": "mark_complete", "args": {}}],
                }
            ],
        },
    )
    flow = load_flow(src)
    out = tmp_path / "out.yaml"
    save_flow(flow, out)
    serialized = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert serialized["version"] == "2.0"
    assert serialized["task_graph"][0]["output_path"] == "out.md"
    assert serialized["task_graph"][0]["postconditions"] == [
        {"name": "mark_complete", "args": {}}
    ]


def test_save_flow_stays_v1_when_no_v2_fields(tmp_path: Path):
    src = tmp_path / "v1.yaml"
    _write_yaml(
        src,
        {
            "version": "1.0",
            "name": "v1",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {"id": "x", "assigned_to": "a", "description": "do"}
            ],
        },
    )
    flow = load_flow(src)
    out = tmp_path / "out.yaml"
    save_flow(flow, out)
    serialized = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert serialized["version"] == "1.0"
