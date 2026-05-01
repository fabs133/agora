import pytest

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification
from agora.core.errors import AgentError
from agora.core.task import Task
from agora.core.types import AgentRole
from agora.fleet.dispatcher import Dispatcher


def _identity(name: str, role: AgentRole) -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"@{name}:agora.local",
        room_id=f"!{name}:agora.local",
        config=AgentConfig(name=name, role=role),
    )


def _task(tid: str, agent_id: str | None = None) -> Task:
    return Task(id=tid, spec=Specification(), agent_id=agent_id)


def test_empty_dispatcher_raises() -> None:
    with pytest.raises(AgentError):
        Dispatcher([])


def test_assign_by_name() -> None:
    agents = [
        _identity("alice", AgentRole.ARCHITECT),
        _identity("bob", AgentRole.IMPLEMENTER),
    ]
    dispatcher = Dispatcher(agents)
    out = dispatcher.assign(_task("t1", agent_id="bob"))
    assert out.config.name == "bob"


def test_assign_by_role_picks_least_loaded() -> None:
    agents = [
        _identity("impl1", AgentRole.IMPLEMENTER),
        _identity("impl2", AgentRole.IMPLEMENTER),
    ]
    dispatcher = Dispatcher(agents)
    a = dispatcher.assign_by_role(_task("t1"), AgentRole.IMPLEMENTER)
    b = dispatcher.assign_by_role(_task("t2"), AgentRole.IMPLEMENTER)
    # With equal load to start, Kahn/deterministic picks first; second call picks the other.
    assert {a.config.name, b.config.name} == {"impl1", "impl2"}


def test_assign_role_string_routes_through() -> None:
    agents = [_identity("t1", AgentRole.TESTER)]
    dispatcher = Dispatcher(agents)
    out = dispatcher.assign(_task("t1", agent_id="tester"))
    assert out.config.name == "t1"


def test_no_suitable_agent_raises() -> None:
    agents = [_identity("a", AgentRole.ARCHITECT)]
    dispatcher = Dispatcher(agents)
    with pytest.raises(AgentError):
        dispatcher.assign(_task("t1", agent_id="ghost"))


def test_release_decrements_load() -> None:
    agents = [_identity("a", AgentRole.ARCHITECT)]
    dispatcher = Dispatcher(agents)
    agent = dispatcher.assign(_task("t1"))
    dispatcher.release(agent)
    # Still resolvable after release.
    assert dispatcher.assign(_task("t2")).config.name == "a"


def test_multiple_agents_without_hint_raises() -> None:
    agents = [
        _identity("a", AgentRole.ARCHITECT),
        _identity("b", AgentRole.IMPLEMENTER),
    ]
    dispatcher = Dispatcher(agents)
    with pytest.raises(AgentError, match="no agent_id"):
        dispatcher.assign(_task("t1"))
