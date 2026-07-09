"""The re-derivation validator — the exchange trust core (L2-1).

A submission is mergeable iff its claimed vector re-derives from its own records.
Tampering with a number (without also fabricating coherent records) is rejected.
"""

from __future__ import annotations

from agora.exchange.validate import looks_like_digest, validate_submission
from tests.exchange._fixtures import make_submission


def test_honest_submission_validates_clean() -> None:
    manifest, attestation, runs, tasks = make_submission()
    assert validate_submission(manifest, attestation, runs, tasks) == []


def test_tampered_vector_value_is_rejected() -> None:
    manifest, attestation, runs, tasks = make_submission()
    # Hand-edit a claimed pass_rate downward — the records still say it passed.
    row = next(r for r in manifest.rows if r.sub_target == "pass_rate")
    row.raw_value = 0.5
    problems = validate_submission(manifest, attestation, runs, tasks)
    assert any("pass_rate" in p and "re-derived" in p for p in problems)


def test_extra_claimed_row_is_rejected() -> None:
    manifest, attestation, runs, tasks = make_submission()
    ghost = manifest.rows[0].model_copy(update={"sub_target": "invented_metric", "raw_value": 1.0})
    manifest.rows.append(ghost)
    problems = validate_submission(manifest, attestation, runs, tasks)
    assert any("do not re-derive" in p for p in problems)


def test_attestation_digest_must_match_manifest() -> None:
    manifest, attestation, runs, tasks = make_submission()
    attestation.model_digest = "sha256:ffff9999eeee"
    problems = validate_submission(manifest, attestation, runs, tasks)
    assert any("model_digest" in p for p in problems)


def test_implausible_duration_is_flagged() -> None:
    manifest, attestation, runs, tasks = make_submission(duration=0.0)
    problems = validate_submission(manifest, attestation, runs, tasks)
    assert any("duration" in p for p in problems)


def test_bad_digest_shape_is_flagged() -> None:
    manifest, attestation, runs, tasks = make_submission()
    manifest.model_digest = "not-a-digest"
    attestation.model_digest = "not-a-digest"
    problems = validate_submission(manifest, attestation, runs, tasks)
    assert any("plausible digest" in p for p in problems)


def test_digest_shape_helper() -> None:
    assert looks_like_digest("sha256:aaaa1111bbbb")
    assert looks_like_digest("aaaa1111bbbbccccdddd")
    assert not looks_like_digest("nope")
    assert not looks_like_digest("")
