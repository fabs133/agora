from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.git.commit import CommitMetadata, CommitStrategy, format_message
from agora.git.repo_manager import RepoManager


def test_format_message_includes_all_fields() -> None:
    meta = CommitMetadata(
        agent_name="architect",
        task_id="t-1",
        task_description="design module",
        phase="architecture",
        fingerprint="abc123",
    )
    msg = format_message(meta)
    assert msg.startswith("[architect] design module")
    assert "Task: t-1" in msg
    assert "Phase: architecture" in msg
    assert "Contract: abc123" in msg


def test_format_message_minimal() -> None:
    meta = CommitMetadata(agent_name="a", task_id="t", task_description="d")
    msg = format_message(meta)
    assert "Phase:" not in msg
    assert "Contract:" not in msg


def test_commit_with_structured_metadata(repo: RepoManager) -> None:
    Path(repo.repo_path, "note.md").write_text("hi", encoding="utf-8")
    strategy = CommitStrategy(repo)
    meta = CommitMetadata(
        agent_name="impl",
        task_id="t-7",
        task_description="add note",
        phase="implementation",
        fingerprint="deadbeef",
    )
    sha = strategy.commit(meta)
    commit = repo.repo.commit(sha)
    assert commit.author.name == "impl"
    assert commit.author.email == "impl@agora.local"
    assert "[impl] add note" in commit.message
    assert "Phase: implementation" in commit.message


def test_commit_specific_files_only(repo: RepoManager) -> None:
    Path(repo.repo_path, "a.txt").write_text("a", encoding="utf-8")
    Path(repo.repo_path, "b.txt").write_text("b", encoding="utf-8")
    strategy = CommitStrategy(repo)
    meta = CommitMetadata(agent_name="x", task_id="t", task_description="partial")
    strategy.commit(meta, files=["a.txt"])
    # b.txt must remain untracked/uncommitted.
    assert "b.txt" in repo.repo.untracked_files


def test_commit_empty_files_list_raises(repo: RepoManager) -> None:
    strategy = CommitStrategy(repo)
    meta = CommitMetadata(agent_name="x", task_id="t", task_description="d")
    with pytest.raises(AgoraError, match="at least one file"):
        strategy.commit(meta, files=[])


def test_commit_without_changes_raises(repo: RepoManager) -> None:
    strategy = CommitStrategy(repo)
    meta = CommitMetadata(agent_name="x", task_id="t", task_description="d")
    with pytest.raises(AgoraError, match="nothing to commit"):
        strategy.commit(meta)


def test_agent_author_sanitizes_email(repo: RepoManager) -> None:
    actor = CommitStrategy.agent_author("Alice Bob")
    assert actor.email == "Alice-Bob@agora.local"
