from pathlib import Path

from agora.git.merge import MergeManager, MergeResult, MergeStrategy
from agora.git.repo_manager import DEFAULT_BRANCH, RepoManager
from tests.git.conftest import commit_file


def _agent_work(repo: RepoManager, agent: str, task: str, relpath: str, content: str) -> str:
    """Create a branch for an agent and add one commit on it. Returns branch name."""
    repo.checkout(DEFAULT_BRANCH)
    branch = repo.create_agent_branch(agent, task)
    commit_file(repo, relpath, content, f"{agent}: work on {task}")
    return branch


def test_no_branches_is_success(repo: RepoManager) -> None:
    manager = MergeManager(repo)
    result = manager.merge_agent_branches()
    assert isinstance(result, MergeResult)
    assert result.success is True
    assert result.merged_branches == []


def test_squash_merge_non_conflicting_branches(repo: RepoManager) -> None:
    _agent_work(repo, "alice", "t1", "alice.txt", "hi from alice")
    _agent_work(repo, "bob", "t2", "bob.txt", "hi from bob")

    manager = MergeManager(repo)
    result = manager.merge_agent_branches(strategy=MergeStrategy.SQUASH)
    assert result.success is True
    assert set(result.merged_branches) == {"agent/alice/t1", "agent/bob/t2"}

    repo.checkout(DEFAULT_BRANCH)
    assert (Path(repo.repo_path) / "alice.txt").read_text() == "hi from alice"
    assert (Path(repo.repo_path) / "bob.txt").read_text() == "hi from bob"


def test_full_merge_preserves_history(repo: RepoManager) -> None:
    _agent_work(repo, "alice", "t1", "alice.txt", "a")
    _agent_work(repo, "bob", "t2", "bob.txt", "b")

    manager = MergeManager(repo)
    result = manager.merge_agent_branches(strategy=MergeStrategy.FULL)
    assert result.success is True

    repo.checkout(DEFAULT_BRANCH)
    messages = [c.message for c in repo.repo.iter_commits(max_count=10)]
    # Full-merge creates merge commits with our "merge agent/..." message.
    assert any("merge agent/alice/t1" in m for m in messages)
    assert any("merge agent/bob/t2" in m for m in messages)


def test_conflict_is_reported_not_auto_resolved(repo: RepoManager) -> None:
    # Two branches modify the same file differently.
    _agent_work(repo, "alice", "t1", "shared.txt", "alice version\n")
    _agent_work(repo, "bob", "t2", "shared.txt", "bob version\n")

    manager = MergeManager(repo)
    result = manager.merge_agent_branches(strategy=MergeStrategy.SQUASH)
    assert result.success is False
    assert len(result.merged_branches) == 1  # first one merges, second conflicts
    assert len(result.conflict_branches) == 1

    # Repo must be in a clean state after conflict handling (no in-progress merge).
    assert not (Path(repo.repo_path) / ".git" / "MERGE_HEAD").exists()


def test_merge_order_by_first_commit_time(repo: RepoManager) -> None:
    import time

    _agent_work(repo, "first", "t1", "a.txt", "1")
    time.sleep(1.1)  # ensure distinct author timestamps (1s resolution)
    _agent_work(repo, "second", "t2", "b.txt", "2")

    manager = MergeManager(repo)
    order = manager.get_merge_order(["agent/second/t2", "agent/first/t1"])
    assert order == ["agent/first/t1", "agent/second/t2"]


def test_merge_specific_branches_only(repo: RepoManager) -> None:
    _agent_work(repo, "alice", "t1", "a.txt", "a")
    _agent_work(repo, "bob", "t2", "b.txt", "b")

    manager = MergeManager(repo)
    result = manager.merge_agent_branches(
        strategy=MergeStrategy.SQUASH, branches=["agent/alice/t1"]
    )
    assert result.success is True
    assert result.merged_branches == ["agent/alice/t1"]

    repo.checkout(DEFAULT_BRANCH)
    assert (Path(repo.repo_path) / "a.txt").exists()
    assert not (Path(repo.repo_path) / "b.txt").exists()
