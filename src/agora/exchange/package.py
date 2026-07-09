"""Package a bench output dir into a submission (capability-program L2-1).

``agora contribute`` drives this. It derives the manifest from a completed run
dir, gathers the attestation, SANITIZES the records, runs the SAME validator the
exchange CI runs (fail early, locally), and — unless dry-run — writes the
submission into the standard layout. gh/PR is the CLI's job; this returns the
prepared submission so the command can dry-run, print the scrub report, and only
then write.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SubmissionResult:
    manifest: Any
    attestation: Any
    problems: list[str] = field(default_factory=list)
    scrub_report: dict[str, int] = field(default_factory=dict)
    submission_dir: Path | None = None
    written: bool = False


def _digest12(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


def _layout(dest: str | Path, manifest: Any, contributor: str, date: str) -> Path:
    """contributions/<digest12>/<battery>@p<probe>/<contributor>-<date>-<shortid>/."""
    shortid = hashlib.sha256(
        json.dumps(manifest.model_dump(), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:8]
    who = (contributor or "anon").strip() or "anon"
    leaf = f"{who}-{date or 'undated'}-{shortid}"
    return (
        Path(dest)
        / "contributions"
        / _digest12(manifest.model_digest)
        / f"{manifest.battery_version}@p{manifest.probe_version}"
        / leaf
    )


def _earliest_date(run_records: list[Any]) -> str:
    starts = [str(getattr(r, "started_at", "") or "") for r in run_records]
    return (min(s for s in starts if s) or "")[:10] if any(starts) else ""


def _write_jsonl_gz(path: Path, records: list[Any]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec.model_dump_json() + "\n")


def package_submission(
    output_dir: str | Path,
    *,
    model_digest: str,
    battery_version: str,
    contributor: str = "",
    dest: str | Path = "dist/exchange",
    attestation_extra: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> SubmissionResult:
    """Prepare (and, unless ``dry_run``, write) a submission from a bench run dir."""
    from agora.bench.matrix import derive_matrix_rows
    from agora.exchange.sanitize import sanitize_submission
    from agora.exchange.schema import Attestation, build_manifest
    from agora.exchange.validate import validate_submission
    from agora.observe.analysis import build_runs_df, build_tasks_df, load_run_records

    runs, tasks, plan = load_run_records(output_dir)
    if not runs:
        raise ValueError(f"no run records under {output_dir}")
    stamped = [
        r.model_copy(
            update={
                "model_digest": r.model_digest or model_digest,
                "battery_version": r.battery_version or battery_version,
            }
        )
        for r in runs
    ]
    runs_df = build_runs_df(stamped, plan=plan, campaign_name="submission")
    tasks_df = build_tasks_df(tasks, runs_df)
    manifest = build_manifest(derive_matrix_rows(stamped, tasks_df, runs_df))

    attestation = Attestation(
        model_digest=model_digest,
        battery_version=battery_version,
        daemon_version=str(getattr(stamped[0], "ollama_version", "") or ""),
        contributor=contributor,
        **(attestation_extra or {}),
    )

    s_runs, s_tasks, s_att, report = sanitize_submission(stamped, tasks, attestation)
    problems = validate_submission(manifest, s_att, s_runs, s_tasks)

    submission_dir = _layout(dest, manifest, contributor, _earliest_date(runs))
    result = SubmissionResult(
        manifest=manifest,
        attestation=s_att,
        problems=problems,
        scrub_report=report,
        submission_dir=submission_dir,
    )
    if not dry_run and not problems:
        _write_submission(submission_dir, manifest, s_att, s_runs, s_tasks)
        result.written = True
    return result


def _write_submission(where: Path, manifest: Any, attestation: Any, runs: list[Any], tasks: list[Any]) -> None:
    import yaml

    where.mkdir(parents=True, exist_ok=True)
    (where / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(), sort_keys=False), encoding="utf-8"
    )
    (where / "attestation.yaml").write_text(
        yaml.safe_dump(attestation.model_dump(), sort_keys=False), encoding="utf-8"
    )
    _write_jsonl_gz(where / "runs.jsonl.gz", runs)
    _write_jsonl_gz(where / "tasks.jsonl.gz", tasks)


__all__ = ["SubmissionResult", "package_submission"]
