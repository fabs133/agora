"""Sanitizer: private tokens out, vector-affecting fields untouched (L2-1)."""

from __future__ import annotations

from agora.exchange.sanitize import private_tokens, sanitize_submission, scrub_report_lines
from agora.exchange.schema import Attestation
from tests.exchange._fixtures import BATTERY, DIGEST, make_records


def _att() -> Attestation:
    return Attestation(model_digest=DIGEST, battery_version=BATTERY, daemon_version="0.1.0", gpu="Tesla P40")


def test_scrubs_named_tokens_and_reports() -> None:
    runs, tasks = make_records()  # host="p40-box"
    s_runs, _s_tasks, _s_att, report = sanitize_submission(
        runs, tasks, _att(), tokens={"p40-box": "[HOST]"}
    )
    assert all(r.host == "[HOST]" for r in s_runs)
    assert report.get("p40-box") == len(s_runs)
    blob = " ".join(r.model_dump_json() for r in s_runs)
    assert "p40-box" not in blob  # zero machine-private strings remain


def test_clean_when_no_tokens_present() -> None:
    runs, tasks = make_records()
    _, _, _, report = sanitize_submission(runs, tasks, _att(), tokens={"nonexistent-string": "[X]"})
    assert report == {}
    assert scrub_report_lines(report) == ["no machine-private strings found"]


def test_sanitizing_does_not_change_the_vector() -> None:
    # Only host/user/home strings are touched — the re-derivation is unchanged.
    from agora.bench.matrix import derive_matrix_rows
    from agora.observe.analysis import build_runs_df, build_tasks_df

    runs, tasks = make_records(digest=DIGEST, battery=BATTERY)
    s_runs, s_tasks, _s_att, _r = sanitize_submission(runs, tasks, _att(), tokens={"p40-box": "[HOST]"})

    def vec(rr, tt):
        rdf = build_runs_df(rr, campaign_name="x")
        col = derive_matrix_rows(rr, build_tasks_df(tt, rdf), rdf)["raw_value"]
        return col.fillna(-999).tolist()  # nan-normalize so nan == nan compares

    assert vec(runs, tasks) == vec(s_runs, s_tasks)


def test_private_tokens_cover_home_user_host() -> None:
    labels = set(private_tokens().values())
    assert "[HOME]" in labels  # home is always resolvable
