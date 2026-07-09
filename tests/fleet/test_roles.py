"""roles.yaml loader + requirement model (L1-C)."""

from __future__ import annotations

import pytest

from agora.core.errors import AgoraError
from agora.fleet.roles import RoleRequirement, load_roles


def test_loads_shipped_roles() -> None:
    rs = load_roles("roles.yaml")
    impl = rs.role("implementer")
    assert isinstance(impl.requires, RoleRequirement)
    assert impl.measured is not None
    assert impl.measured.probe == "tool-call-fidelity"
    assert impl.measured.min["pass_rate"] == 1.0
    # The determinism cap is separated from the genuine sub_target thresholds.
    assert impl.measured.sub_target_minimums == {"pass_rate": 1.0}
    assert impl.measured.repeat_distinct_max == 1
    # The role's harness is what its matrix key is derived from.
    assert impl.harness["tool_errors"] == "corrective"


def test_unmeasured_and_task_specific_roles_have_no_requirement() -> None:
    rs = load_roles("roles.yaml")
    assert rs.role("planner").requires == "unmeasured"
    assert rs.role("planner").measured is None
    assert rs.role("classifier").requires == "task_specific"
    assert rs.role("classifier").measured is None


def test_unknown_role_raises() -> None:
    with pytest.raises(AgoraError, match="unknown role"):
        load_roles("roles.yaml").role("nope")
