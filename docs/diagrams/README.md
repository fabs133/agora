# Diagrams

PlantUML sources (`.puml`) and rendered SVGs for the architectural docs.
Sources are canonical; SVGs are committed for direct embedding in Sphinx.

| Source | Rendered | What it shows |
|--------|----------|---------------|
| [architecture.puml](architecture.puml) | [architecture.svg](architecture.svg) | Component overview: Runner → Orchestration → Inner-tool layer + LLM backends + Observer surface, plus Git + workspace artefact stores. |
| [project_phases.puml](project_phases.puml) | [project_phases.svg](project_phases.svg) | Project phase state machine — INIT → ANALYSIS → ARCHITECTURE → IMPLEMENTATION → TESTING → REVIEW → DONE/FAILED. Loopback paths in blue, failure paths in red. Mirrors `VALID_TRANSITIONS` in [`agora/core/project.py`](../../src/agora/core/project.py). |
| [task_status.puml](task_status.puml) | [task_status.svg](task_status.svg) | Task status state machine — PENDING → ASSIGNED → RUNNING → REVIEW → DONE/FAILED. Mirrors `VALID_TASK_TRANSITIONS` in [`agora/core/task.py`](../../src/agora/core/task.py). |
| [task_sequence.puml](task_sequence.puml) | [task_sequence.svg](task_sequence.svg) | Sequence of one `AgentRuntime.execute_task` call — system prompt composition, tool-call loop, auto-hooks on writes, postcondition evaluation, learning synthesis on failure. |

## Regenerating

The SVGs in this directory were rendered through the
`gamedev-mcp` server's `plantuml_render` tool. To regenerate after
editing a `.puml` source, either:

- Drive the same MCP tool from a Claude Code session, or
- Render directly against a local PlantUML server:
  ```bash
  cat docs/diagrams/<name>.puml | curl --data-binary @- \
    -H "Content-Type: text/plain" \
    "http://localhost:18080/svg" > docs/diagrams/<name>.svg
  ```
- Or use the official PlantUML jar:
  ```bash
  plantuml -tsvg docs/diagrams/<name>.puml
  ```

## Style notes

- **Package labels.** PlantUML emits an opaque `Bad Request` when a
  `package "X" { ... }` label collides with any component alias in
  the diagram. The convention here is to suffix package labels with
  `*Pkg` (e.g. `as OrchPkg`) so collisions can't happen.
- **Identifiers with special characters.** Wrap component labels
  containing `/`, `.`, or `*` in double quotes:
  `["scripts/run_*.py"] as Runner`. Bare names with hyphens are fine.
- **Color conventions.** Default arrows are dark grey (`#444`). Loop-back
  edges in the state diagrams are blue. Unrecoverable-failure edges are red.
