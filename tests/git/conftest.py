"""Fixtures for git tests. Each test gets a fresh repo in tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.git.repo_manager import RepoManager


@pytest.fixture
def repo(tmp_path: Path) -> RepoManager:
    mgr = RepoManager(tmp_path / "repo")
    mgr.init_project_repo("test-project")
    return mgr


def commit_file(repo: RepoManager, relpath: str, content: str, message: str) -> str:
    path = Path(repo.repo_path) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return repo.commit_all(message)
