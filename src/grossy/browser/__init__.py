"""Browser observation boundary for Grossy (PRD sections 11, 17).

The browser module defines the versioned observation envelope that crosses
the agent-to-CLI boundary, the abstract adapter interface, and the fake
adapter used by offline tests. No real browser or network access occurs in
this module.
"""

from grossy.browser.adapter import (
    AuthCheckResult,
    BrowserAdapter,
    FakeBrowserAdapter,
    HistoryControlsResult,
)
from grossy.browser.observation import (
    CART_OBSERVATION_SCHEMA_NAME,
    CART_OBSERVATION_SCHEMA_VERSION,
    CartObservation,
    CartObservationError,
    CartObservationLine,
    validate_cart_observation,
)

__all__ = [
    "CART_OBSERVATION_SCHEMA_NAME",
    "CART_OBSERVATION_SCHEMA_VERSION",
    "AuthCheckResult",
    "BrowserAdapter",
    "CartObservation",
    "CartObservationError",
    "CartObservationLine",
    "FakeBrowserAdapter",
    "HistoryControlsResult",
    "validate_cart_observation",
]
