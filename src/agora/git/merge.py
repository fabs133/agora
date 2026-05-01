"""Merge strategies for multi-agent branches.

At project completion, all ``agent/*`` branches merge into ``main``.

Two strategies:
- :attr:`MergeStrategy.SQUASH`: each branch becomes one squashed commit on the target
  (clean history; drops intra-branch granularity).
- :attr:`MergeStrategy.FULL`: regular merge, preserving the full commit graph
  (full provenance; history is noisier).

Conflict handling: if a merge conflicts, the working tree is reset cleanly and the
branch is reported as conflicted. **No auto-resolution** — the project is marked for
human review. Ordering: by first-commit author-time of each branch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from git import GitCommandError

from agora.core.errors import AgoraError
from agora.git.repo_manager import DEFAULT_BRANCH, RepoManager

logger = logging.getLogger(__name__)


class MergeStrategy(str, Enum):
    SQUASH = "squash"
    FULL = "full"


@dataclass
class MergeResult:
    success: bool
    merged_branches: list[str] = field(default_factory=list)
    conflict_branches: list[str] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)
    final_commit_sha: str | None = None


class MergeManager:
    def __init__(self, repo: RepoManager) -> None:
        self._repo = repo

    def merge_agent_branches(
        self,
        target_branch: str = DEFAULT_BRANCH,
        strategy: MergeStrategy = MergeStrategy.SQUASH,
        branches: list[str] | None = None,
    ) -> MergeResult:
        """Merge all (or specified) agent branches into ``target_branch``."""
        repo = self._repo.repo
        originally_on = self._repo.get_current_branch()

        target = branches if branches is not None else self._repo.list_agent_branches()
        if not target:
            return MergeResult(success=True, merged_branches=[])

        ordered = self.get_merge_order(target)
        self._repo.checkout(target_branch)

        result = MergeResult(success=True)
        for branch in ordered:
            try:
                if strategy == MergeStrategy.SQUASH:
                    self._squash_merge(branch)
                else:
                    self._full_merge(branch)
                result.merged_branches.append(branch)
            except _MergeConflict as conflict:
                result.success = False
                result.conflict_branches.append(branch)
                result.conflict_files.extend(conflict.files)
                # Bail out — don't keep merging on top of a broken state.
                break
            except AgoraError:
                result.success = False
                result.conflict_branches.append(branch)
                break

        result.final_commit_sha = repo.head.commit.hexsha
        # Restore caller's starting branch when safe.
        try:
            self._repo.checkout(originally_on)
        except GitCommandError:
            pass
        return result

    # -------------------------------------------------------------------- helpers

    def get_merge_order(self, branches: list[str]) -> list[str]:
        """Order by the author-time of each branch's *first branch-unique* commit."""
        repo = self._repo.repo

        def first_unique_commit_time(branch: str) -> int:
            try:
                commits = list(repo.iter_commits(f"{DEFAULT_BRANCH}..{branch}"))
            except Exception:  # noqa: BLE001
                commits = list(repo.iter_commits(branch))
            if not commits:
                return 0
            # iter_commits walks newest → oldest; last is the earliest branch commit.
            return int(commits[-1].authored_date)

        return sorted(branches, key=first_unique_commit_time)

    # ----- strategy implementations -----

    def _squash_merge(self, branch: str) -> None:
        repo = self._repo.repo
        try:
            repo.git.merge("--squash", branch)
        except GitCommandError as exc:
            files = _extract_conflict_files(repo)
            repo.git.reset("--merge")
            raise _MergeConflict(files) from exc
        # --squash leaves changes staged but does not commit.
        try:
            repo.index.commit(f"squash merge {branch}")
        except Exception as exc:  # noqa: BLE001
            raise AgoraError(f"squash commit failed for {branch}: {exc}") from exc

    def _full_merge(self, branch: str) -> None:
        repo = self._repo.repo
        try:
            repo.git.merge("--no-ff", "-m", f"merge {branch}", branch)
        except GitCommandError as exc:
            files = _extract_conflict_files(repo)
            # `merge --abort` is safer than reset --merge when a merge commit is in progress.
            try:
                repo.git.merge("--abort")
            except GitCommandError:
                repo.git.reset("--merge")
            raise _MergeConflict(files) from exc


class _MergeConflict(Exception):
    def __init__(self, files: list[str]) -> None:
        self.files = files
        super().__init__(f"merge conflict in {files}")


def _extract_conflict_files(repo) -> list[str]:
    try:
        status = repo.git.status("--porcelain")
    except GitCommandError:
        return []
    conflicts: list[str] = []
    for line in status.splitlines():
        # Unmerged markers: UU, AA, DD, AU, UA, DU, UD.
        if line[:2].strip() and line[0] in "UAD" and line[1] in "UAD":
            conflicts.append(line[3:].strip())
    return conflicts
