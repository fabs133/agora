"""Capture each workspace/<run>/.git history to a flat git-log.txt, then strip
the inner .git directories so the parent repo can ship cleanly.

Run once before the initial public commit. Idempotent: re-running on a tree
where inner .git dirs are already gone is a no-op.

Output per run dir:
    workspace/<run>/git-log.txt    full log: hash, ISO date, author, message,
                                   plus per-commit name-status (file changes)
    workspace/<run>/git-refs.txt   branch + tag list at strip time

Then: rm -rf workspace/<run>/.git
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _force_remove(func, path, exc_info):
    """rmtree onexc handler: clear readonly bit (git pack files) then retry."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace"

LOG_FORMAT = "commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n    %s%n%n%b"


def capture_log(git_dir: Path) -> tuple[str, str]:
    log = subprocess.run(
        [
            "git",
            f"--git-dir={git_dir}",
            "log",
            "--all",
            f"--format={LOG_FORMAT}",
            "--name-status",
            "--date=iso-strict",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    refs = subprocess.run(
        [
            "git",
            f"--git-dir={git_dir}",
            "for-each-ref",
            "--format=%(refname:short)\t%(objectname:short)\t%(authordate:iso-strict)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return log.stdout, refs.stdout


def main() -> int:
    if not WORKSPACE.exists():
        print(f"no workspace/ directory at {WORKSPACE}", file=sys.stderr)
        return 1

    inner_gits = sorted(p for p in WORKSPACE.glob("*/.git") if p.is_dir())
    if not inner_gits:
        print("no inner .git directories found — nothing to do")
        return 0

    print(f"found {len(inner_gits)} inner .git directories")

    captured = []
    for git_dir in inner_gits:
        parent = git_dir.parent
        log_text, refs_text = capture_log(git_dir)

        if not log_text.strip():
            print(f"  WARN: empty log for {parent.name} — skipping strip")
            continue

        log_path = parent / "git-log.txt"
        refs_path = parent / "git-refs.txt"
        log_path.write_text(log_text, encoding="utf-8")
        refs_path.write_text(refs_text, encoding="utf-8")

        commit_count = log_text.count("\ncommit ") + (1 if log_text.startswith("commit ") else 0)
        captured.append((git_dir, commit_count, log_path.stat().st_size))
        print(f"  {parent.name}: {commit_count} commits -> {log_path.name} ({log_path.stat().st_size} B)")

    print(f"\ncaptured {len(captured)} of {len(inner_gits)} inner repos")
    if len(captured) != len(inner_gits):
        print("  refusing to strip with empty captures present", file=sys.stderr)
        return 2

    print("\nstripping inner .git directories...")
    for git_dir, _, _ in captured:
        # Python 3.12+: onexc; older: onerror. Try the new name first.
        try:
            shutil.rmtree(git_dir, onexc=_force_remove)
        except TypeError:
            shutil.rmtree(git_dir, onerror=_force_remove)
        print(f"  rm -rf {git_dir.relative_to(REPO_ROOT)}")

    print(f"\ndone. {len(captured)} inner .git dirs stripped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
