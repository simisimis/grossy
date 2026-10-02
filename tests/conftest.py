"""Shared test-only expectations and fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def current_schema_version() -> int:
    """Expected schema version; bump this when adding a shipped migration.

    Keep this expectation independent of ``discover_migrations()`` so a missing
    migration cannot make both implementation and expectation agree by accident.
    """
    return 8
