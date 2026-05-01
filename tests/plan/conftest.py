"""Shared fixtures for plan tests.

The predicate registry is global mutable state populated at import time. Tests
that add/remove entries via ``register_predicate`` must restore the snapshot
after the test, else test ordering can leak state.
"""

from __future__ import annotations

import pytest

from agora.plan import predicate_registry


@pytest.fixture
def registry_snapshot():
    """Snapshot + restore the predicate registry around a test."""
    # ``_REGISTRY`` is a module-level dict — shallow-copy suffices since
    # factory values are bound module-level functions.
    original = dict(predicate_registry._REGISTRY)
    yield predicate_registry._REGISTRY
    predicate_registry._REGISTRY.clear()
    predicate_registry._REGISTRY.update(original)
