"""Tests for the fake browser adapter (PRD sections 9, 17).

Verifies that the FakeBrowserAdapter returns pre-defined results without
any network access, browser launch, or cart mutation. All tests are
fully offline.
"""

from __future__ import annotations

from grossy.browser.adapter import (
    AuthCheckResult,
    BrowserAdapter,
    FakeBrowserAdapter,
    HistoryControlsResult,
)
from grossy.browser.observation import (
    CART_OBSERVATION_SCHEMA_NAME,
    CartObservation,
    CartObservationLine,
)


class TestFakeAdapterDefaults:
    def test_is_browser_adapter(self) -> None:
        adapter = FakeBrowserAdapter()
        assert isinstance(adapter, BrowserAdapter)

    def test_default_auth_authenticated(self) -> None:
        adapter = FakeBrowserAdapter()
        result = adapter.check_authentication()
        assert result.authenticated is True
        assert "fake" in result.detail

    def test_default_history_visible(self) -> None:
        adapter = FakeBrowserAdapter()
        result = adapter.check_history_controls()
        assert result.visible is True
        assert "fake" in result.detail

    def test_default_cart_valid_and_repeatable(self) -> None:
        adapter = FakeBrowserAdapter()
        cart = adapter.read_cart()
        assert isinstance(cart, CartObservation)
        assert cart.schema_name == CART_OBSERVATION_SCHEMA_NAME
        assert cart.schema_version == 1
        assert len(cart.lines) == 1
        assert cart.lines[0].product_id == "1234567"
        assert cart.lines[0].quantity == 1
        assert adapter.read_cart() == cart


class TestFakeAdapterCustom:
    """Configured results are returned verbatim on every call."""

    def test_custom_auth_unauthenticated(self) -> None:
        auth = AuthCheckResult(False, "session expired")
        adapter = FakeBrowserAdapter(auth=auth)
        result = adapter.check_authentication()
        assert result.authenticated is False
        assert result.detail == "session expired"
        assert adapter.check_authentication() == auth

    def test_custom_history_not_visible(self) -> None:
        history = HistoryControlsResult(False, "UI changed")
        adapter = FakeBrowserAdapter(history=history)
        result = adapter.check_history_controls()
        assert result.visible is False
        assert result.detail == "UI changed"
        assert adapter.check_history_controls() == history

    def test_custom_cart_empty(self) -> None:
        cart = CartObservation(
            schema_name=CART_OBSERVATION_SCHEMA_NAME,
            schema_version=1,
            generated_at="2026-09-26T12:00:00Z",
            lines=[],
        )
        adapter = FakeBrowserAdapter(cart=cart)
        result = adapter.read_cart()
        assert result.lines == []

    def test_custom_cart_multiple_lines(self) -> None:
        cart = CartObservation(
            schema_name=CART_OBSERVATION_SCHEMA_NAME,
            schema_version=1,
            generated_at="2026-09-26T12:00:00Z",
            lines=[
                CartObservationLine(
                    product_id="111",
                    canonical_url="https://store.example/en/eshop/A/B/p/111",
                    quantity=2,
                    displayed_price_eur="5.00",
                ),
                CartObservationLine(
                    product_id="222",
                    canonical_url="https://store.example/en/eshop/C/D/p/222",
                    quantity=1,
                    displayed_price_eur="10.50",
                ),
            ],
        )
        adapter = FakeBrowserAdapter(cart=cart)
        result = adapter.read_cart()
        assert len(result.lines) == 2
        assert result.lines[0].product_id == "111"
        assert result.lines[1].product_id == "222"
