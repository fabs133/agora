"""Sprint 7.5(a) + v2.8 (Approach C — C4b): loader remaps impl tasks to
api_spec modules; raises when mismatched tasks can't be remapped.

v2.8 change: the prior behaviour silently DROPPED a task when more impl
tasks existed than spec modules. Observed live-run failure was a
``cli_entry_point`` task vanishing because the architect forgot to list
``src/cli.py`` in api_spec, and the executor then shipped a broken plan
missing its CLI. The new behaviour raises :class:`AgoraError` with a
structured message so the finalize_plan framework stage surfaces it as
an ERROR and the architect's retry loop (via the author_tasks task's
C4a-side validation) sees the problem at authoring time.
"""

from __future__ import annotations

import pytest

from agora.core.errors import AgoraError
from agora.core.flow import PostconditionRef, TaskTemplate
from agora.plan.loader import _align_impl_tasks_to_spec


def _impl(id_: str, src_path: str) -> TaskTemplate:
    """Build a minimal impl task with py_compiles(rel=<src_path>)."""
    return TaskTemplate(
        id=id_,
        assigned_to="implementer",
        description="implement",
        depends_on=(),
        output_path=src_path,
        postconditions=(
            PostconditionRef(
                name="file_exists", args=(("rel", src_path),)
            ),
            PostconditionRef(
                name="py_compiles", args=(("rel", src_path),)
            ),
            PostconditionRef(name="mark_complete", args=()),
        ),
    )


_ONE_MODULE_SPEC = "## module: src/cli.py\n\ndef foo() -> None: ...\n"


def test_align_noop_when_spec_empty():
    tasks = [_impl("a", "src/x.py")]
    out = _align_impl_tasks_to_spec(tasks, "")
    assert out == tasks


def test_align_noop_when_task_already_matches_spec():
    tasks = [_impl("a", "src/cli.py")]
    out = _align_impl_tasks_to_spec(tasks, _ONE_MODULE_SPEC)
    assert len(out) == 1
    assert out[0].output_path == "src/cli.py"


def test_align_remaps_mismatched_task_to_unused_spec_module():
    # Plan emitted an impl task targeting src/domain.py but spec declares
    # only src/cli.py. Remap to cli.py.
    tasks = [_impl("add_core", "src/domain.py")]
    out = _align_impl_tasks_to_spec(tasks, _ONE_MODULE_SPEC)
    assert len(out) == 1
    assert out[0].output_path == "src/cli.py"
    # Postconditions follow.
    rels = [
        dict(pc.args).get("rel", "") for pc in out[0].postconditions
    ]
    assert "src/cli.py" in rels
    assert "src/domain.py" not in rels


def test_align_raises_when_spec_exhausted():
    """v2.8(C4b): two impl tasks + one-module spec → raise, don't drop.

    The prior silent-drop behaviour hid the architect's mistake. Now we
    raise with a structured message the finalize stage surfaces."""
    tasks = [
        _impl("cli_task", "src/cli.py"),
        _impl("extra_task", "src/domain.py"),
    ]
    with pytest.raises(AgoraError) as excinfo:
        _align_impl_tasks_to_spec(tasks, _ONE_MODULE_SPEC)
    msg = str(excinfo.value)
    # Names the offending task + the spec it's missing from.
    assert "extra_task" in msg
    assert "src/domain.py" in msg
    # Surfaces the known-modules set for context.
    assert "src/cli.py" in msg
    # Includes the fix hint.
    assert "fix:" in msg.lower()


def test_align_raises_even_with_dependents():
    """v2.8(C4b): raising happens before the dependent-rewiring step,
    so dependents aren't silently corrupted either."""
    dependent = TaskTemplate(
        id="downstream",
        assigned_to="tester",
        description="test",
        depends_on=("cli_task", "extra_task"),
        output_path="tests/test_contract.py",
        postconditions=(
            PostconditionRef(name="file_exists", args=(("rel", "tests/test_contract.py"),)),
        ),
    )
    tasks = [
        _impl("cli_task", "src/cli.py"),
        _impl("extra_task", "src/domain.py"),
        dependent,
    ]
    with pytest.raises(AgoraError, match="extra_task"):
        _align_impl_tasks_to_spec(tasks, _ONE_MODULE_SPEC)


def test_align_raises_lists_all_bad_tasks():
    """Multiple unmappable tasks → all appear in the message so the user
    sees the complete violation set, not just the first one."""
    tasks = [
        _impl("t1", "src/cli.py"),  # aligned
        _impl("t2", "src/bad_a.py"),
        _impl("t3", "src/bad_b.py"),
    ]
    with pytest.raises(AgoraError) as excinfo:
        _align_impl_tasks_to_spec(tasks, _ONE_MODULE_SPEC)
    msg = str(excinfo.value)
    assert "t2" in msg
    assert "t3" in msg
    assert "src/bad_a.py" in msg
    assert "src/bad_b.py" in msg


def test_align_uses_two_spec_modules_for_two_impl_tasks():
    spec = (
        "## module: src/cli.py\n\ndef a() -> None: ...\n\n"
        "## module: src/domain.py\n\ndef b() -> None: ...\n"
    )
    tasks = [
        _impl("impl_first", "src/wrong_a.py"),
        _impl("impl_second", "src/wrong_b.py"),
    ]
    out = _align_impl_tasks_to_spec(tasks, spec)
    assert [t.output_path for t in out] == ["src/cli.py", "src/domain.py"]


def test_align_respects_already_matched_first_then_remaps_rest():
    spec = (
        "## module: src/cli.py\n\ndef a() -> None: ...\n\n"
        "## module: src/domain.py\n\ndef b() -> None: ...\n"
    )
    # First task already matches spec; second is mismatched — it should
    # remap to the REMAINING (unused) spec module.
    tasks = [
        _impl("good", "src/cli.py"),
        _impl("mismatched", "src/other.py"),
    ]
    out = _align_impl_tasks_to_spec(tasks, spec)
    assert [t.output_path for t in out] == ["src/cli.py", "src/domain.py"]


def test_align_ignores_non_production_spec_modules_and_raises():
    """Spec modules under src/tests/ or tests/ must NOT be used as remap
    targets — they're test files, not production code. v2.8(C4b): the
    unmappable extra task now raises instead of silently dropping."""
    spec = (
        "## module: src/cli.py\n\ndef a() -> None: ...\n\n"
        "## module: src/tests/test_x.py\n\n"
        "def test_y() -> None: ...\n"
    )
    tasks = [
        _impl("impl_task", "src/wrong.py"),  # remaps to src/cli.py
        _impl("extra_task", "src/other.py"),  # no production module left → raise
    ]
    with pytest.raises(AgoraError, match="extra_task"):
        _align_impl_tasks_to_spec(tasks, spec)


def test_align_remaps_single_task_even_with_test_module_in_spec():
    """Regression guard: a single impl task still remaps to the one
    production module in a spec that ALSO contains a test module section.
    No extra tasks → no raise."""
    spec = (
        "## module: src/cli.py\n\ndef a() -> None: ...\n\n"
        "## module: src/tests/test_x.py\n\n"
        "def test_y() -> None: ...\n"
    )
    tasks = [_impl("impl_task", "src/wrong.py")]
    out = _align_impl_tasks_to_spec(tasks, spec)
    paths = [t.output_path for t in out]
    assert paths == ["src/cli.py"]


def test_align_skips_non_src_tasks():
    """Setup-style tasks that don't write src/*.py are left alone."""
    non_src = TaskTemplate(
        id="setup",
        assigned_to="architect",
        description="setup",
        depends_on=(),
        output_path="requirements.txt",
        postconditions=(
            PostconditionRef(name="file_exists", args=(("rel", "requirements.txt"),)),
        ),
    )
    tasks = [non_src, _impl("cli_task", "src/cli.py")]
    out = _align_impl_tasks_to_spec(tasks, _ONE_MODULE_SPEC)
    assert len(out) == 2
    assert out[0].output_path == "requirements.txt"
