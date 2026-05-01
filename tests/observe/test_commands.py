from agora.observe.commands import (
    VERB_ABORT,
    VERB_HELP,
    VERB_NOTE,
    VERB_REDIRECT,
    VERB_REVIEW,
    parse_command,
    validate,
)


def test_non_agora_message_returns_none() -> None:
    assert parse_command("hello world") is None
    assert parse_command("") is None
    assert parse_command("   /agoranot-prefixed") is None
    assert parse_command(None) is None  # type: ignore[arg-type]


def test_bare_agora_is_help() -> None:
    cmd = parse_command("/agora")
    assert cmd is not None and cmd.verb == VERB_HELP


def test_note_with_args() -> None:
    cmd = parse_command("/agora note focus on error handling", sender="@fabs:agora.local")
    assert cmd is not None
    assert cmd.verb == VERB_NOTE
    assert cmd.args == ("focus", "on", "error", "handling")
    assert cmd.sender == "@fabs:agora.local"
    assert cmd.argline == "focus on error handling"


def test_note_requires_text() -> None:
    cmd = parse_command("/agora note")
    assert cmd is not None
    ok, reason = validate(cmd)
    assert ok is False
    assert "note" in reason


def test_redirect_requires_agent_and_text() -> None:
    cmd = parse_command("/agora redirect architect focus on modularity")
    assert cmd is not None
    assert cmd.verb == VERB_REDIRECT
    ok, _ = validate(cmd)
    assert ok is True

    bad = parse_command("/agora redirect architect")
    assert bad is not None
    ok, reason = validate(bad)
    assert ok is False and "agent" in reason


def test_review_requires_exactly_one_id() -> None:
    cmd = parse_command("/agora review approve")
    assert cmd is not None
    assert cmd.verb == VERB_REVIEW
    ok, _ = validate(cmd)
    assert ok is True

    bad = parse_command("/agora review approve reject")
    assert bad is not None
    ok, reason = validate(bad)
    assert ok is False


def test_abort_optional_reason() -> None:
    cmd = parse_command("/agora abort")
    assert cmd is not None and cmd.verb == VERB_ABORT
    cmd2 = parse_command("/agora abort tests failed")
    assert cmd2 is not None and cmd2.args == ("tests", "failed")


def test_unknown_verb_falls_back_to_help() -> None:
    cmd = parse_command("/agora banana peel")
    assert cmd is not None
    assert cmd.verb == VERB_HELP
    # Unknown verb is preserved in args for useful error messages.
    assert "banana" in cmd.args


def test_malformed_quotes_does_not_crash() -> None:
    # shlex would raise without our fallback; just assert we return something.
    cmd = parse_command('/agora note "unbalanced quote')
    assert cmd is not None
    assert cmd.verb == VERB_NOTE


def test_handles_leading_whitespace() -> None:
    cmd = parse_command("   /agora help   ")
    assert cmd is not None and cmd.verb == VERB_HELP
