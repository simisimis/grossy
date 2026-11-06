"""Abstract browser adapter interface and fake implementation (PRD sections 9, 17).

The adapter is the boundary between the Pi skill agent and the browser.
All browser-returned data must cross this boundary as a validated
``CartObservation`` envelope. The fake adapter is used by offline tests
and never makes network requests or launches a browser.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from grossy.browser.observation import (
    CART_OBSERVATION_SCHEMA_NAME,
    CartObservation,
    CartObservationLine,
)
from grossy.store import store_base_url


@dataclass(frozen=True)
class AuthCheckResult:
    """Result of an authentication check."""

    authenticated: bool
    detail: str


@dataclass(frozen=True)
class HistoryControlsResult:
    """Result of a history-controls visibility check."""

    visible: bool
    detail: str


class BrowserAdapter(ABC):
    """Abstract interface for browser operations used by the doctor.

    Implementations must be read-only: they must never mutate the cart,
    enter credentials, or progress checkout.
    """

    @abstractmethod
    def check_authentication(self) -> AuthCheckResult:
        """Check whether the user is authenticated to the store."""

    @abstractmethod
    def check_history_controls(self) -> HistoryControlsResult:
        """Check whether the rendered order-history controls are visible."""

    @abstractmethod
    def read_cart(self) -> CartObservation:
        """Read the current cart without modifying it."""


def _default_observation() -> CartObservation:
    """Return a minimal valid cart observation for fake adapter defaults."""
    return CartObservation(
        schema_name=CART_OBSERVATION_SCHEMA_NAME,
        schema_version=1,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        lines=[
            CartObservationLine(
                product_id="1234567",
                canonical_url=f"{store_base_url()}/en/eshop/Dairy/Yogurt/p/1234567",
                quantity=1,
                displayed_price_eur="3.49",
            )
        ],
    )


class FakeBrowserAdapter(BrowserAdapter):
    """Fake browser adapter for offline tests.

    Returns pre-defined results without making any network requests
    or launching a browser. Safe for all test scenarios.
    """

    def __init__(
        self,
        auth: AuthCheckResult | None = None,
        history: HistoryControlsResult | None = None,
        cart: CartObservation | None = None,
    ) -> None:
        self._auth = auth or AuthCheckResult(True, "authenticated (fake)")
        self._history = history or HistoryControlsResult(True, "visible (fake)")
        self._cart = cart or _default_observation()

    def check_authentication(self) -> AuthCheckResult:
        return self._auth

    def check_history_controls(self) -> HistoryControlsResult:
        return self._history

    def read_cart(self) -> CartObservation:
        return self._cart
