<!-- One paragraph: what changed and why. Lead with the failure mode or
     research finding that motivated this — not the implementation. -->

## Summary

## Motivation

<!-- For framework gates / postconditions: link the run that surfaced the
     failure. For research: link the publishable thread or experiment design. -->

## Test plan

- [ ] `pytest --cov=agora --cov-fail-under=80 -q` passes locally
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] If a new gate landed: a regression test that fails without it
- [ ] If `docs/lessons-learned.md` round table grew: the new row cites the run that drove the change

## Operating principles checklist

<!-- Tick what applies, leave the rest unchecked. See
     docs/lessons-learned.md § Operating principles. -->

- [ ] Every new postcondition is driven by a real observed failure (not speculative)
- [ ] No regression in test count or coverage gate
- [ ] Touches one logical change — not bundled with unrelated cleanup
- [ ] Cross-platform impact considered (single-machine validation reality)
