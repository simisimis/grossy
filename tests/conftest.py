"""Shared test-only expectations and fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from grossy.store import STORE_URL_ENV

# Fake store base URL used by every test; no real store domain appears in tests.
TEST_STORE_URL = "https://store.example"


@pytest.fixture(scope="session", autouse=True)
def store_url_env() -> Iterator[None]:
    """Point ``GROSSY_STORE_URL`` at the test store for the whole session.

    Tests that need a missing or different value override it with the
    function-scoped ``monkeypatch`` fixture, which restores this value afterwards.
    """
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setenv(STORE_URL_ENV, TEST_STORE_URL)
        yield


@pytest.fixture(scope="session")
def current_schema_version() -> int:
    """Expected schema version; bump this when adding a shipped migration.

    Keep this expectation independent of ``discover_migrations()`` so a missing
    migration cannot make both implementation and expectation agree by accident.
    """
    return 11
