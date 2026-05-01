"""Git repository lifecycle: init, branch-per-agent, checkout, diff, log.

Wraps GitPython. The ``Repo`` object may be injected for testing, but the default
path is straightforward: open an existing repo at ``repo_path`` or ``init`` it.

Branch naming convention for agent work: ``agent/<name>/<task-id>``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from git import Actor, InvalidGitRepositoryError, NoSuchPathError, Repo

from agora.core.errors import AgoraError

logger = logging.getLogger(__name__)

DEFAULT_BRANCH = "main"
_BRANCH_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_fragment(value: str) -> str:
    """Normalize an arbitrary string into a git-ref-safe fragment."""
    cleaned = _BRANCH_SAFE.sub("-", value).strip("-")
    return cleaned or "x"


class RepoManager:
    """Thin wrapper around a :class:`git.Repo` with Agora-specific conventions."""

    def __init__(self, repo_path: str | Path, repo: Repo | None = None) -> None:
        self.repo_path = Path(repo_path)
        if repo is not None:
            self._repo = repo
            return
        try:
            self._repo = Repo(str(self.repo_path))
        except (InvalidGitRepositoryError, NoSuchPathError):
            self._repo = None  # type: ignore[assignment]

    # ---------------------------------------------------------------- lifecycle

    def init_project_repo(self, project_name: str) -> None:
        """Initialize a fresh repo with a root commit on ``main``."""
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self._repo = Repo.init(str(self.repo_path), initial_branch=DEFAULT_BRANCH)

        readme = self.repo_path / "README.md"
        if not readme.exists():
            readme.write_text(f"# {project_name}\n", encoding="utf-8")
        gitignore = self.repo_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("__pycache__/\n.venv/\n.coverage\n", encoding="utf-8")

        self._repo.index.add(["README.md", ".gitignore"])
        author = Actor("agora", "agora@agora.local")
        self._repo.index.commit(
            f"chore: initialize {project_name}", author=author, committer=author
        )

    @property
    def repo(self) -> Repo:
        if self._repo is None:
            raise AgoraError(f"no git repo at {self.repo_path}")
        return self._repo

    # ----------------------------------------------------------------- branches

    def create_agent_branch(self, agent_name: str, task_id: str) -> str:
        """Create and checkout ``agent/<name>/<task-id>``. Returns the branch name."""
        branch_name = f"agent/{_safe_fragment(agent_name)}/{_safe_fragment(task_id)}"
        repo = self.repo
        if branch_name in [h.name for h in repo.heads]:
            repo.git.checkout(branch_name)
            return branch_name
        repo.git.checkout("-b", branch_name)
        return branch_name

    def checkout(self, branch: str) -> None:
        self.repo.git.checkout(branch)

    def list_agent_branches(self) -> list[str]:
        return sorted(h.name for h in self.repo.heads if h.name.startswith("agent/"))

    def get_current_branch(self) -> str:
        return self.repo.active_branch.name

    def has_uncommitted_changes(self) -> bool:
        return self.repo.is_dirty(untracked_files=True)

    # ---------------------------------------------------- agent-runtime helpers

    def commit_all(
        self,
        message: str,
        agent_name: str = "agora",
        email_domain: str = "agora.local",
    ) -> str:
        """Stage all changes (tracked + untracked) and commit. Returns commit SHA.

        Used by the agent runtime's ``git_commit`` tool; for structured commits use
        :class:`~agora.git.commit.CommitStrategy`.
        """
        repo = self.repo
        repo.git.add(A=True)
        if not self.has_uncommitted_changes() and not repo.index.diff("HEAD"):
            raise AgoraError("nothing to commit")
        author = Actor(agent_name, f"{_safe_fragment(agent_name)}@{email_domain}")
        commit = repo.index.commit(message, author=author, committer=author)
        return commit.hexsha

    def diff(self, ref: str | None = None) -> str:
        """Unified diff of working tree vs HEAD (or vs ``ref``)."""
        if ref is None:
            return self.repo.git.diff()
        return self.repo.git.diff(ref)

    def log(self, limit: int = 10, branch: str | None = None) -> str:
        """Return ``git log`` output for the active or specified branch."""
        args = ["--oneline", f"-n{limit}"]
        if branch:
            args.append(branch)
        return self.repo.git.log(*args)
