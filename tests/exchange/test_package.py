"""Packager: dry-run validates; --write materializes a clean, re-loadable submission (L2-1)."""

from __future__ import annotations

import gzip
import socket

from agora.exchange.package import package_submission
from tests.exchange._fixtures import BATTERY, DIGEST, make_records


def _write_run_dir(where, *, host=None):
    where.mkdir(parents=True, exist_ok=True)
    runs, tasks = make_records()  # no digest/battery — the packager stamps
    if host is not None:
        runs = [r.model_copy(update={"host": host}) for r in runs]
    (where / "run.jsonl").write_text("\n".join(r.model_dump_json() for r in runs), encoding="utf-8")
    (where / "tasks.jsonl").write_text("\n".join(t.model_dump_json() for t in tasks), encoding="utf-8")
    return where


def test_dry_run_validates_and_writes_nothing(tmp_path) -> None:
    src = _write_run_dir(tmp_path / "run")
    res = package_submission(
        src, model_digest=DIGEST, battery_version=BATTERY, contributor="octocat",
        dest=tmp_path / "dist", dry_run=True,
    )
    assert res.problems == []
    assert not res.written
    assert res.submission_dir is not None and not res.submission_dir.exists()
    assert res.manifest.model_digest == DIGEST


def test_write_materializes_a_valid_reloadable_submission(tmp_path) -> None:
    src = _write_run_dir(tmp_path / "run")
    res = package_submission(
        src, model_digest=DIGEST, battery_version=BATTERY, contributor="octocat",
        dest=tmp_path / "dist", dry_run=False,
    )
    assert res.written and res.problems == []
    d = res.submission_dir
    for name in ("manifest.yaml", "attestation.yaml", "runs.jsonl.gz", "tasks.jsonl.gz"):
        assert (d / name).exists(), name
    # The layout is keyed by digest / battery@probe.
    assert DIGEST.removeprefix("sha256:")[:12] in str(d)
    assert f"{BATTERY}@p7" in str(d)


def test_written_submission_carries_no_real_machine_host(tmp_path) -> None:
    # Inject the REAL hostname into the records; the packager must scrub it.
    host = socket.gethostname()
    src = _write_run_dir(tmp_path / "run", host=host)
    res = package_submission(
        src, model_digest=DIGEST, battery_version=BATTERY, contributor="octocat",
        dest=tmp_path / "dist", dry_run=False,
    )
    assert res.scrub_report.get(host, 0) > 0  # it was found + redacted
    body = gzip.open(res.submission_dir / "runs.jsonl.gz", "rt", encoding="utf-8").read()
    assert host not in body  # zero machine-private strings in the written file
