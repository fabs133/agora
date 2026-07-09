"""Benchmark pipeline (roles-and-casting Stage 3 / capability-program Layer 1).

Turns campaign JSONL into a keyed, re-derivable capability matrix. The vector
math lives in :mod:`agora.observe.layer2`; this package adds the re-derivable
KEY (:mod:`agora.bench.keys`) and the canonical CSV matrix store
(:mod:`agora.bench.matrix`). See docs/design/capability-program-plan.md.
"""
