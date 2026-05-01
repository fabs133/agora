"""Agent-attributed commits with structured commit messages.

Commit message format::

    [<agent-name>] <task description>

    Task: <task-id>
    Phase: <project-phase>
    Contract: <specification-fingerprint>
"""

from __future__ import annotations

from dataclasses import dataclass

from git import Actor

from agora.core.errors import AgoraError
from agora.git.repo_manager import RepoManager, _safe_fragment


@dataclass(frozen=True)
class CommitMetadata:
    agent_name: str
    task_id: str
    task_description: str
    phase: str = ""
    fingerprint: str = ""
    email_domain: str = "agora.local"


def format_message(meta: CommitMetadata) -> str:
    header = f"[{meta.agent_name}] {meta.task_description}".strip()
    body_lines = [f"Task: {meta.task_id}"]
    if meta.phase:
        body_lines.append(f"Phase: {meta.phase}")
    if meta.fingerprint:
        body_lines.append(f"Contract: {meta.fingerprint}")
    return header + "\n\n" + "\n".join(body_lines) + "\n"


class CommitStrategy:
    """Create structured, attributed commits on the current branch."""

    def __init__(self, repo: RepoManager) -> None:
        self._repo = repo

    @staticmethod
    def agent_author(agent_name: str, email_domain: str = "agora.local") -> Actor:
        return Actor(agent_name, f"{_safe_fragment(agent_name)}@{email_domain}")

    def commit(
        self,
        meta: CommitMetadata,
        files: list[str] | None = None,
    ) -> str:
        """Stage the given files (or all changes) and commit. Returns commit SHA."""
        repo = self._repo.repo
        if files is not None:
            if not files:
                raise AgoraError("commit requires at least one file when files is provided")
            repo.index.add(files)
        else:
            repo.git.add(A=True)

        # Nothing to commit guard: diff against HEAD plus untracked check.
        try:
            diff_vs_head = repo.index.diff("HEAD")
        except Exception:  # noqa: BLE001 - repo with no HEAD yet
            diff_vs_head = None
        if not diff_vs_head and not self._repo.has_uncommitted_changes():
            raise AgoraError("nothing to commit")

        author = self.agent_author(meta.agent_name, meta.email_domain)
        commit = repo.index.commit(
            format_message(meta), author=author, committer=author
        )
        return commit.hexsha
