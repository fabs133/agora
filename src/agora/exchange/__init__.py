"""Capability exchange (capability-program Layer 2) — in-repo machinery.

The distribution layer for the bench matrix. This package holds the parts that
live in the agora repo (so the local packager and the exchange CI share ONE
validator and cannot drift): the submission schema (:mod:`agora.exchange.schema`),
the re-derivation validator (:mod:`agora.exchange.validate` — the trust core: a
claimed vector must re-derive from its own raw records), the private-string
sanitizer (:mod:`agora.exchange.sanitize`), and the packager
(:mod:`agora.exchange.package`, driven by ``agora contribute``).

The exchange REPO itself (agora-capability-exchange) + its CI + the data license
are created outside this repo — see docs/design/capability-exchange.md.
"""
