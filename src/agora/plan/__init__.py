"""Plan package — declarative v2.0 plan format layered on top of :mod:`agora.core.flow`.

A plan is a Flow (existing YAML template with agents + task DAG) extended with:

- Typed postcondition references resolved via :mod:`agora.plan.predicate_registry`.
- Optional stage templates for per-task staged execution.
- ``output_path`` hints that travel with the task.

The loader returns the triple ``(agents, tasks, staged_tasks)`` that
:meth:`agora.fleet.orchestrator.Orchestrator.run_project` already consumes —
no orchestrator changes required.
"""

from agora.plan.loader import Plan, instantiate_plan, load_plan, save_plan
from agora.plan.predicate_registry import (
    build_predicate,
    list_registered_predicates,
    register_predicate,
)

__all__ = [
    "Plan",
    "build_predicate",
    "instantiate_plan",
    "list_registered_predicates",
    "load_plan",
    "register_predicate",
    "save_plan",
]
