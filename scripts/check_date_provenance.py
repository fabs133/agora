"""Date-provenance verifier for the run-history archive.

Scans the archive's hand-written / extracted documents for date strings
and duration claims, and cross-references them against workspace
filesystem evidence (git commit ranges from registry.yaml).  Flags any
date that does not resolve to:

* A workspace git commit timestamp (per registry.yaml).
* The repo creation date (taken as 2026-04-15 unless overridden).
* Today's date (taken from --today, default current local date).

Use case: defensive pattern paired with the cost-provenance schema in
registry.yaml.  Caught the "10 weeks (2026-02 → 2026-04)" fabrication
in the original plan draft (2026-04-26).

Usage::

    python scripts/check_date_provenance.py
    python scripts/check_date_provenance.py --today 2026-04-26 --strict
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_RUNS = REPO_ROOT / "docs" / "runs"
LESSONS_LEARNED = REPO_ROOT / "docs" / "lessons-learned.md"
DEFAULT_REPO_CREATED = date(2026, 4, 15)

# ISO date pattern (YYYY-MM-DD); restricted to plausible 2025/2026 era to
# avoid false positives on version strings like 1090 etc.
ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

# Duration claims we want to sanity-check.  Numeric prefix + unit.
DURATION_RE = re.compile(
    r"\b(\d+)\s+(day|days|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)


def _load_git_dates(registry_path: Path) -> tuple[date | None, date | None]:
    if not registry_path.exists():
        return None, None
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    firsts: list[date] = []
    lasts: list[date] = []
    for r in data.get("runs", []):
        g = r.get("git", {}) or {}
        for key, bucket in (("first_iso", firsts), ("last_iso", lasts)):
            iso = g.get(key)
            if not iso:
                continue
            try:
                bucket.append(datetime.fromisoformat(iso).date())
            except ValueError:
                pass
    if not firsts or not lasts:
        return None, None
    return min(firsts), max(lasts)


def _files_to_check(extra: list[Path]) -> list[Path]:
    files: list[Path] = []
    if LESSONS_LEARNED.exists():
        files.append(LESSONS_LEARNED)
    if DOCS_RUNS.exists():
        for p in sorted(DOCS_RUNS.iterdir()):
            if p.is_file() and p.suffix in {".md", ".yaml"} and not p.name.startswith("_"):
                files.append(p)
    for p in extra:
        if p.exists():
            files.append(p)
    return files


def check_dates(
    files: list[Path],
    git_first: date | None,
    git_last: date | None,
    repo_created: date,
    today: date,
    grace_days: int = 1,
) -> list[tuple[Path, int, str, str]]:
    """Return a list of (path, line_no, date_string, reason) tuples for
    every ISO date that does not resolve to filesystem evidence."""
    if git_first is None or git_last is None:
        # Fall back to repo creation -> today range.
        git_first = repo_created
        git_last = today
    valid_lo = git_first - timedelta(days=grace_days)
    valid_hi = git_last + timedelta(days=grace_days)
    # Today is also a valid reference for dates in archive notes (when the
    # archival itself was generated).
    valid_dates = {repo_created, today}
    issues: list[tuple[Path, int, str, str]] = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for m in ISO_DATE_RE.finditer(line):
                ds = m.group(1)
                try:
                    d = datetime.strptime(ds, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if d in valid_dates:
                    continue
                if valid_lo <= d <= valid_hi:
                    continue
                issues.append((f, lineno, ds, f"outside git range [{valid_lo} .. {valid_hi}] and not repo-created/today"))
    return issues


def check_durations(
    files: list[Path],
    git_first: date | None,
    git_last: date | None,
    repo_created: date,
    today: date,
) -> list[tuple[Path, int, str, str]]:
    """Flag any duration claim that exceeds the project lifetime."""
    if git_first is None:
        git_first = repo_created
    project_age = (today - git_first).days + 1  # inclusive
    issues: list[tuple[Path, int, str, str]] = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for m in DURATION_RE.finditer(line):
                n = int(m.group(1))
                unit = m.group(2).lower().rstrip("s")
                days = {"day": n, "week": n * 7, "month": n * 30, "year": n * 365}[unit]
                # Allow durations only if they fit the project lifetime.
                if days > project_age * 1.2:  # 20% slack for inclusive-vs-exclusive ambiguity
                    issues.append((
                        f,
                        lineno,
                        f"{n} {unit}s",
                        f"exceeds project lifetime ({project_age} days)",
                    ))
    return issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--today", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=date.today())
    ap.add_argument(
        "--repo-created",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=DEFAULT_REPO_CREATED,
    )
    ap.add_argument("--strict", action="store_true", help="exit non-zero on any unresolved date")
    ap.add_argument("--extra", type=Path, action="append", default=[], help="extra file to check")
    args = ap.parse_args(argv)

    files = _files_to_check(args.extra)
    if not files:
        print("No archive files to check.", file=sys.stderr)
        return 0

    git_first, git_last = _load_git_dates(DOCS_RUNS / "registry.yaml")
    print(
        f"Validity range: git=[{git_first} .. {git_last}], "
        f"repo_created={args.repo_created}, today={args.today}",
        file=sys.stderr,
    )

    date_issues = check_dates(files, git_first, git_last, args.repo_created, args.today)
    dur_issues = check_durations(files, git_first, git_last, args.repo_created, args.today)

    if not date_issues and not dur_issues:
        print(f"OK — checked {len(files)} files, no unresolved dates or implausible durations.")
        return 0

    if date_issues:
        print(f"\n{len(date_issues)} unresolved date(s):")
        for f, ln, ds, reason in date_issues:
            print(f"  {f.relative_to(REPO_ROOT)}:{ln}: {ds} — {reason}")
    if dur_issues:
        print(f"\n{len(dur_issues)} implausible duration claim(s):")
        for f, ln, ds, reason in dur_issues:
            print(f"  {f.relative_to(REPO_ROOT)}:{ln}: {ds} — {reason}")

    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
