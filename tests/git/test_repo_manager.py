from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.git.repo_manager import DEFAULT_BRANCH, RepoManager
from tests.git.conftest import commit_file


def test_init_creates_repo_with_initial_commit(tmp_path: Path) -> None:
    mgr = RepoManager(tmp_path / "fresh")
    mgr.init_project_repo("demo")
    assert (tmp_path / "fresh" / "README.md").is_file()
    assert (tmp_path / "fresh" / ".gitignore").is_file()
    assert mgr.get_current_branch() == DEFAULT_BRANCH
    assert len(list(mgr.repo.iter_commits())) == 1


def test_create_agent_branch_naming(repo: RepoManager) -> None:
    branch = repo.create_agent_branch("alice", "t-42")
    assert branch == "agent/alice/t-42"
    assert repo.get_current_branch() == branch


def test_create_agent_branch_sanitizes_weird_input(repo: RepoManager) -> None:
    branch = repo.create_agent_branch("Alice / Bob", "feat spec!!")
    assert branch.startswith("agent/Alice-Bob/feat-spec")


def test_create_agent_branch_reenters_existing(repo: RepoManager) -> None:
    b1 = repo.create_agent_branch("alice", "t1")
    repo.checkout(DEFAULT_BRANCH)
    b2 = repo.create_agent_branch("alice", "t1")
    assert b1 == b2
    assert repo.get_current_branch() == b1


def test_list_agent_branches_filters(repo: RepoManager) -> None:
    repo.create_agent_branch("alice", "t1")
    repo.checkout(DEFAULT_BRANCH)
    repo.create_agent_branch("bob", "t2")
    branches = repo.list_agent_branches()
    assert branches == ["agent/alice/t1", "agent/bob/t2"]


def test_commit_all_and_log(repo: RepoManager) -> None:
    sha = commit_file(repo, "src/foo.py", "print('hi')\n", "feat: add foo")
    assert len(sha) == 40
    log = repo.log(limit=5)
    assert "feat: add foo" in log


def test_commit_all_with_no_changes_raises(repo: RepoManager) -> None:
    with pytest.raises(AgoraError, match="nothing to commit"):
        repo.commit_all("empty")


def test_diff_shows_uncommitted(repo: RepoManager) -> None:
    Path(repo.repo_path, "README.md").write_text("# changed\n", encoding="utf-8")
    assert "changed" in repo.diff()


def test_has_uncommitted_changes(repo: RepoManager) -> None:
    assert not repo.has_uncommitted_changes()
    Path(repo.repo_path, "x.txt").write_text("new", encoding="utf-8")
    assert repo.has_uncommitted_changes()


def test_open_missing_repo_raises_on_access(tmp_path: Path) -> None:
    mgr = RepoManager(tmp_path / "nowhere")
    with pytest.raises(AgoraError, match="no git repo"):
        _ = mgr.repo
