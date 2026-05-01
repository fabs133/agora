# Contributing to Agora

Thanks for the interest. Before opening a PR, please skim the [project log](docs/lessons-learned.md) — Agora is opinionated about what it accepts, and most of the friction in early contributions comes from not knowing the operating principles up front.

## Operating principles

These came out of 18 rounds of empirical hardening and apply to every change:

1. **Every postcondition / gate / predicate is driven by a real observed failure.** "Speculative postconditions" — guards added without an observed run that motivated them — are explicitly out of scope. They add friction without evidence of value. If you want to add a gate, name the run that surfaced the gap (or run an experiment to surface one).

2. **The framework is at the asymptote for 7B.** Diminishing returns on more 7B-specific scaffolding. Before proposing a fix, ask: "would this same capability just work at 14B?" If yes, it's a model issue, not a framework issue, and the right venue is the model, not Agora.

3. **Don't regress the test count or coverage gate.** Currently 1095 tests at 80%+ coverage. A refactor that drops below either is a refactor that destroyed evidence.

4. **Touch one logical change.** Bundling a bug fix with surrounding cleanup makes review noisy and bisect impossible. Keep cleanup PRs separate from feature PRs.

## Development setup

```bash
git clone https://github.com/fabs133/agora && cd agora
python -m venv .venv
source .venv/bin/activate            # POSIX
# .venv\Scripts\activate             # Windows
pip install -e ".[dev,litellm,llm,docs]"
```

Verify:

```bash
ruff check .
ruff format --check .
pytest --cov=agora --cov-fail-under=80 --timeout=90 -q
```

End-to-end live tests are gated behind `AGORA_E2E=1` and need Conduit + Ollama running. For most contributions you don't need them.

## Workflow

1. Open an issue first if the change is non-trivial. The [issue templates](.github/ISSUE_TEMPLATE/) ask the right questions.
2. Branch off `main`, name your branch by intent: `gate/api-spec-bullets`, `fix/edit-loop-error-formatting`, `research/cross-tier-ablation`.
3. One PR = one logical change. If you find unrelated dirt, open a separate cleanup PR.
4. Tests for new behaviour go alongside it (`tests/<module>/test_<name>.py`).
5. If you added a gate that catches a new failure mode, also append a row to the round table in `docs/lessons-learned.md` citing the run.

## Commit messages

Conventional but light:

```
<scope>: <imperative verb> <what>

<paragraph if non-obvious why>
```

Examples that fit:

- `plan: add api_spec_no_stray_bullets predicate`
- `fleet: format edit-loop matches with line numbers + 1-line context`
- `runs: archive plan-builder.run15 (gpt-4o-mini failure mode)`

Avoid `chore:` or `style:` prefixes. If it's worth committing, it's worth describing.

## Documentation

- Code-level: type hints carry their own weight; only add a docstring or comment when *why* is non-obvious.
- Project-level: round-by-round changes go in [docs/lessons-learned.md](docs/lessons-learned.md). Per-run findings go in [docs/runs/](docs/runs/).
- API reference: built automatically by Sphinx from type hints in `src/agora/`. If you change a public surface, the docs rebuild on the next CI run.

## Reporting research findings

Different from bug reports — see the [research issue template](.github/ISSUE_TEMPLATE/03-research.yml). Empirical contributions (cross-tier comparisons, replication on different hardware, capability-ceiling probes) are first-class. Tell us the hypothesis, the design, and what would settle it.

## Code of conduct

Be precise, be honest about what's evidenced vs hypothesised, and don't waste reviewers' time with speculative changes. That's the whole code of conduct.

## License

By contributing, you agree your contributions are licensed under [Apache-2.0](LICENSE) — same as the rest of the project.
