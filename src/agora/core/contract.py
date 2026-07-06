"""Manifold Specification Pattern.

Position: the ground-truth layer of the whole framework. A task's postconditions —
not the model's ``mark_complete`` self-report — decide whether it passed; the
orchestrator and the phase gate evaluate these predicates after the agent's turn.
Every downstream verification (local gates F10, re-runnable ``run_check`` records
F20, phase-0 re-validation) is a predicate evaluated through this shape.

A ``Specification`` combines preconditions (must hold before execution) and
postconditions (must hold after). Each specification has a deterministic
fingerprint derived from its content; identical fingerprints drive retry
deduplication in later sprints.

Predicates are callables of shape ``(context: dict) -> (passed: bool, reason: str)``.
They are pure — they do not mutate the context (so re-evaluating a spec over a
workspace is side-effect-free, which is what makes mechanical gate re-eval safe).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agora.core.types import Fingerprint

PredicateFn = Callable[[dict[str, Any]], tuple[bool, str]]


@dataclass(frozen=True)
class Predicate:
    """A named condition evaluated against a context dict."""

    name: str
    description: str
    evaluate: PredicateFn = field(compare=False)


@dataclass(frozen=True)
class Specification:
    """Pre/post conditions that gate task execution.

    A ``Specification`` is attached to each :class:`~agora.core.task.Task`.
    The framework evaluates ``preconditions`` before the task runs and
    ``postconditions`` after; a task is considered ``success`` iff every
    postcondition evaluates ``True``. The LLM's ``mark_complete`` call is
    observed but does not determine outcome — postconditions are the ground
    truth.

    All fields are immutable; the ``fingerprint`` property derives a stable
    SHA-256 identity from ``description`` plus the canonicalised predicate
    set. Identical fingerprints across two specifications mean they evaluate
    the same conditions, which the framework uses to deduplicate retries
    and learnings.
    """

    preconditions: tuple[Predicate, ...] = ()
    postconditions: tuple[Predicate, ...] = ()
    description: str = ""

    @property
    def fingerprint(self) -> Fingerprint:
        """SHA-256 over canonical content: description + sorted predicate identities."""
        parts: list[str] = [f"desc={self.description}"]
        for label, preds in (("pre", self.preconditions), ("post", self.postconditions)):
            tokens = sorted(f"{p.name}|{p.description}" for p in preds)
            parts.append(f"{label}=" + ";".join(tokens))
        canonical = "\n".join(parts).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def evaluate_preconditions(
    spec: Specification, context: dict[str, Any]
) -> list[tuple[str, str]]:
    """Return ``[(predicate_name, failure_reason), ...]`` for failed preconditions."""
    return _evaluate(spec.preconditions, context)


def evaluate_postconditions(
    spec: Specification, context: dict[str, Any]
) -> list[tuple[str, str]]:
    """Return ``[(predicate_name, failure_reason), ...]`` for failed postconditions."""
    return _evaluate(spec.postconditions, context)


def _evaluate(
    preds: tuple[Predicate, ...], context: dict[str, Any]
) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for pred in preds:
        passed, reason = pred.evaluate(context)
        if not passed:
            failures.append((pred.name, reason))
    return failures


def make_predicate(name: str, description: str, check: PredicateFn) -> Predicate:
    """Convenience constructor for predicates."""
    return Predicate(name=name, description=description, evaluate=check)
