"""Versioned cart observation envelope (PRD section 11, family 5).

The ``grossy.cart.observation.v1`` schema is the structured data that crosses
the browser observation boundary into the deterministic CLI. It is a closed
schema: unknown fields are rejected, and string values are scanned for
prohibited content (executable text, SQL, cookies, headers, DOM, screenshots,
credentials, addresses, account identity).
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from grossy.config.models import NUMERIC_ID_PATTERN, product_id_from_url

CART_OBSERVATION_SCHEMA_NAME = "grossy.cart.observation.v1"
CART_OBSERVATION_SCHEMA_VERSION = 1

# Defense-in-depth: scan string values for obviously dangerous content.
# The primary safety property is the closed schema (extra="forbid"); these
# patterns catch content that could be embedded in legitimate-looking fields.
_PROHIBITED_CONTENT_RE = re.compile(
    r"(<\s*script\b"  # HTML script tag
    r"|javascript\s*:"  # JavaScript URI
    r"|data\s*:\s*image/"  # Base64 image data
    r"|\bSET-COOKIE\s*:"  # HTTP cookie header
    r"|\bSELECT\s+\S+\s+FROM\b"  # SQL SELECT
    r"|\bDROP\s+TABLE\b"  # SQL DROP
    r")",
    re.IGNORECASE,
)


class CartObservationError(ValueError):
    """Raised when a cart observation fails validation."""


class CartObservationLine(BaseModel):
    """One line item in the rendered AB.gr cart."""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern=NUMERIC_ID_PATTERN)
    canonical_url: str
    # StrictInt rejects lax coercion (e.g. the string "2" must not become 2);
    # quantities crossing the browser boundary must be genuine JSON integers.
    quantity: StrictInt = Field(gt=0)
    displayed_price_eur: str

    @field_validator("canonical_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if product_id_from_url(v) is None:
            raise ValueError(
                "canonical_url must be a supported AB.gr product URL "
                "of the form https://www.ab.gr/[en|el/]eshop/.../p/<id>"
            )
        return v

    @field_validator("displayed_price_eur")
    @classmethod
    def _check_price(cls, v: str) -> str:
        try:
            d = Decimal(v)
        except InvalidOperation:
            raise ValueError(f"displayed_price_eur must be a decimal string, got {v!r}") from None
        if not d.is_finite():
            raise ValueError(f"displayed_price_eur must be finite, got {v!r}")
        if d <= 0:
            raise ValueError(f"displayed_price_eur must be positive, got {v!r}")
        return v


class CartObservation(BaseModel):
    """Versioned cart observation envelope (grossy.cart.observation.v1)."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    # StrictInt so a string-typed schema version (e.g. "1") is rejected rather
    # than silently coerced to an integer at the schema boundary.
    schema_version: StrictInt
    generated_at: str
    lines: list[CartObservationLine]

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, v: str) -> str:
        if v != CART_OBSERVATION_SCHEMA_NAME:
            raise ValueError(f"schema_name must be {CART_OBSERVATION_SCHEMA_NAME!r}, got {v!r}")
        return v

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: int) -> int:
        if v != CART_OBSERVATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CART_OBSERVATION_SCHEMA_VERSION}, got {v}")
        return v

    @field_validator("generated_at")
    @classmethod
    def _check_generated_at(cls, v: str) -> str:
        try:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"generated_at must be a valid ISO 8601 timestamp, got {v!r}"
            ) from None
        if parsed.tzinfo is None:
            raise ValueError(
                f"generated_at must be a timezone-aware ISO 8601 instant "
                f"(include a UTC offset such as +00:00 or Z), got {v!r}"
            )
        return v

    @field_validator("lines")
    @classmethod
    def _check_lines(cls, v: list[CartObservationLine]) -> list[CartObservationLine]:
        for line in v:
            url_id = product_id_from_url(line.canonical_url)
            if url_id is not None and url_id != line.product_id:
                raise ValueError(
                    f"product_id {line.product_id!r} disagrees with the ID "
                    f"carried by canonical_url ({url_id!r})"
                )
        return v


def _scan_prohibited(obj: Any) -> None:
    """Recursively scan all string values for prohibited content patterns."""
    if isinstance(obj, str):
        if _PROHIBITED_CONTENT_RE.search(obj):
            raise CartObservationError(f"prohibited content in observation: {obj[:80]!r}")
    elif isinstance(obj, dict):
        for val in obj.values():
            _scan_prohibited(val)
    elif isinstance(obj, list):
        for item in obj:
            _scan_prohibited(item)


def validate_cart_observation(data: dict[str, Any]) -> CartObservation:
    """Validate a raw dict against the cart observation schema.

    Checks:
    1. No prohibited content in any string value.
    2. Schema name and version match exactly.
    3. Required fields present; no unknown fields.
    4. Product IDs are numeric strings.
    5. Canonical URLs are valid AB.gr product URLs.
    6. Product ID agrees with the URL-derived ID.
    7. Quantities are positive strict integers (no string coercion).
    8. Prices are valid positive decimal strings.
    9. schema_version is a strict integer (no string coercion).
    """
    _scan_prohibited(data)
    try:
        return CartObservation.model_validate(data)
    except CartObservationError:
        raise
    except Exception as exc:
        raise CartObservationError(f"cart observation validation failed: {exc}") from exc
