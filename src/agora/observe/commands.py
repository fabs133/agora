"""`/agora <verb> <args>` directive parser.

Human observers type commands in any room. The sync loop routes ``m.room.message``
events to :func:`parse_command`, which returns a :class:`ParsedCommand` or
``None`` (for non-agora messages). The orchestrator registers a handler that
maps verbs to side effects (notes, redirects, pause/resume/abort, review votes).

The grammar is forgiving:

- ``/agora note <text>`` — attach a free-form note to the target room.
- ``/agora pause`` / ``/agora resume`` — toggle the orchestrator's run gate.
- ``/agora abort [reason]`` — cancel the current project.
- ``/agora redirect <agent_name> <new instructions>`` — update agent instructions.
- ``/agora review <answer_id>`` — fallback for clients that can't render polls.
- ``/agora help`` — the renderer responds with a usage block.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

COMMAND_PREFIX = "/agora"

VERB_NOTE = "note"
VERB_PAUSE = "pause"
VERB_RESUME = "resume"
VERB_ABORT = "abort"
VERB_REDIRECT = "redirect"
VERB_REVIEW = "review"
VERB_HELP = "help"
VERB_COMMENT = "comment"
VERB_DECISION = "decision"

VALID_VERBS = frozenset(
    {
        VERB_NOTE, VERB_PAUSE, VERB_RESUME, VERB_ABORT, VERB_REDIRECT,
        VERB_REVIEW, VERB_HELP, VERB_COMMENT, VERB_DECISION,
    }
)


@dataclass(frozen=True)
class ParsedCommand:
    """A parsed ``/agora <verb> <args>`` directive. ``args`` is the whitespace
    split (kept for free-text verbs like ``note``); ``argv`` is the shlex
    tokenization (for verbs with quoted arguments). ``raw``/``sender`` preserve
    provenance for the audit log."""

    verb: str
    args: tuple[str, ...]
    raw: str
    sender: str
    argv: tuple[str, ...] = ()  # shlex-tokenized full argv after verb

    @property
    def argline(self) -> str:
        """The raw argument string after the verb (useful for free-text args)."""
        return " ".join(self.args)


def parse_command(body: str, sender: str = "") -> ParsedCommand | None:
    """Return a :class:`ParsedCommand` if ``body`` starts with ``/agora``, else None.

    Malformed commands (unknown verbs, missing required args) still return a
    ``ParsedCommand`` with ``verb=help``-style fallback — the caller emits usage.
    """
    if not isinstance(body, str):
        return None
    stripped = body.strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    after_prefix = stripped[len(COMMAND_PREFIX) :]
    # Require either end-of-string or whitespace after /agora so that tokens
    # like /agoranot-a-command are *not* treated as commands.
    if after_prefix and not after_prefix[0].isspace():
        return None

    remainder = after_prefix.lstrip()
    if not remainder:
        # Bare `/agora` -> treat as help.
        return ParsedCommand(verb=VERB_HELP, args=(), raw=stripped, sender=sender)

    try:
        tokens = shlex.split(remainder, posix=True)
    except ValueError:
        tokens = remainder.split()

    if not tokens:
        return ParsedCommand(verb=VERB_HELP, args=(), raw=stripped, sender=sender)

    verb = tokens[0].lower()
    args_tuple = tuple(tokens[1:])

    if verb not in VALID_VERBS:
        # Unknown verb — return as-is with verb="help" sentinel so handler prints usage.
        return ParsedCommand(
            verb=VERB_HELP,
            args=(verb, *args_tuple),
            raw=stripped,
            sender=sender,
            argv=(verb, *args_tuple),
        )

    return ParsedCommand(
        verb=verb,
        args=args_tuple,
        raw=stripped,
        sender=sender,
        argv=(verb, *args_tuple),
    )


HELP_TEXT = (
    "Agora commands:\n"
    "  /agora note <text>              — attach a note for agents\n"
    "  /agora pause | /agora resume    — halt / continue the orchestrator\n"
    "  /agora abort [reason]           — cancel the current project\n"
    "  /agora redirect <agent> <text>  — rewrite agent instructions\n"
    "  /agora review <answer_id>       — cast a poll vote without Element\n"
    "  /agora comment <task_id> <text> — attach feedback to a specific task\n"
    "  /agora help                     — this message"
)


def validate(cmd: ParsedCommand) -> tuple[bool, str]:
    """Return ``(ok, reason)``. Use in handlers to emit usage on failure."""
    if cmd.verb == VERB_NOTE and not cmd.args:
        return False, "/agora note requires <text>"
    if cmd.verb == VERB_REDIRECT and len(cmd.args) < 2:
        return False, "/agora redirect requires <agent> <instructions>"
    if cmd.verb == VERB_REVIEW and len(cmd.args) != 1:
        return False, "/agora review requires exactly one <answer_id>"
    if cmd.verb == VERB_COMMENT and len(cmd.args) < 2:
        return False, "/agora comment requires <task_id> <text>"
    if cmd.verb == VERB_DECISION and len(cmd.args) not in (1, 2):
        return False, "/agora decision requires <answer_id> or <decision_id> <answer_id>"
    return True, ""


def as_dict(cmd: ParsedCommand) -> dict[str, Any]:
    return {
        "verb": cmd.verb,
        "args": list(cmd.args),
        "raw": cmd.raw,
        "sender": cmd.sender,
    }
