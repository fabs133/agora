# Agora

**Agora drives a weak or cheap LLM through a DAG of specified tasks to produce
working code end-to-end.** The framework absorbs everything the model is
unreliable at — postcondition gates verify each task instead of trusting the
model's self-report, auto-hooks run the validators the model would forget,
edit primitives prevent transcription bugs, auto-learnings inject failure
traces back into the prompt on retry. The bet: with the right scaffolding,
a 7B local model can ship working code that a naked 7B model cannot.

Five load-bearing ideas, validated across 46 archived runs on three test-bed
projects and four model tiers. See [the project log](lessons-learned.md) for
the round-by-round evolution and [the run archive](runs/README.md) for the
empirical evidence.

```{toctree}
:caption: Getting started
:maxdepth: 2

element-setup
```

```{toctree}
:caption: Project log
:maxdepth: 2

lessons-learned
```

```{toctree}
:caption: Run history
:maxdepth: 2

runs/README
runs/findings
runs/publishable
runs/discord-bot.run13
runs/discord-bot.run3
runs/plan-builder.run14-4omini-clean
runs/url-shortener-mvp.run1-7b-broken
runs/url-shortener-mvp.live
```

```{toctree}
:caption: API reference
:maxdepth: 2

api/index
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
