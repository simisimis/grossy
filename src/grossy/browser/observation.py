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
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from grossy.config.models import NUMERIC_ID_PATTERN, product_id_from_url
from grossy.security import ProhibitedContentError, scan_prohibited_content

CART_OBSERVATION_SCHEMA_NAME = "grossy.cart.observation.v1"
CART_OBSERVATION_SCHEMA_VERSION = 1


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
            raise ValueError("displayed_price_eur must be a decimal string") from None
        if not d.is_finite():
            raise ValueError("displayed_price_eur must be finite")
        if d <= 0:
            raise ValueError("displayed_price_eur must be positive")
        return v


class RecurringProductFact(BaseModel):
    """Current rendered product facts; no guessed stock or historical quantities."""

    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: str = Field(pattern=NUMERIC_ID_PATTERN)
    canonical_url: str
    displayed_price_eur: str | None
    availability: Literal["available", "unavailable", "unknown"]

    @field_validator("displayed_price_eur")
    @classmethod
    def _check_price(cls, value: str | None) -> str | None:
        return CartObservationLine._check_price(value) if value is not None else None

    @model_validator(mode="after")
    def _available_price(self) -> RecurringProductFact:
        if self.availability == "available" and self.displayed_price_eur is None:
            raise ValueError("available product requires an observed price")
        return self


class RecipeProductFact(RecurringProductFact):
    """One adapter unit supplies amount * count; weighed amount is a rendered increment."""

    package_amount: str | None
    package_unit: Literal["g", "kg", "ml", "l", "piece", "pieces", "count"] | None
    package_count: StrictInt | None = Field(gt=0)
    sale_kind: Literal["packaged", "weighed"] | None

    @field_validator("package_amount")
    @classmethod
    def _amount(cls, value: str | None) -> str | None:
        if value is not None and (
            not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) or Decimal(value) <= 0
        ):
            raise ValueError("package amount must be a positive exact decimal string")
        return value

    @model_validator(mode="after")
    def _weighed_count(self) -> RecipeProductFact:
        if self.availability == "available" and any(
            v is None
            for v in (self.package_amount, self.package_unit, self.package_count, self.sale_kind)
        ):
            raise ValueError("available recipe product requires observed package facts")
        if self.sale_kind == "weighed" and self.package_count != 1:
            raise ValueError("weighed increment count must be one")
        return self


class MutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    manifest_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["readback", "failed", "incomplete"]


class CartObservation(BaseModel):
    """Versioned cart observation envelope (grossy.cart.observation.v1)."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    # StrictInt so a string-typed schema version (e.g. "1") is rejected rather
    # than silently coerced to an integer at the schema boundary.
    schema_version: StrictInt
    generated_at: str
    lines: list[CartObservationLine]
    recurring_product_facts: list[RecurringProductFact] = Field(default_factory=list)
    recipe_product_facts: list[RecipeProductFact] = Field(default_factory=list)
    mutation_result: MutationResult | None = None

    @model_validator(mode="after")
    def _check_current_prices(self) -> CartObservation:
        prices = {line.product_id: Decimal(line.displayed_price_eur) for line in self.lines}
        facts = [*self.recurring_product_facts, *self.recipe_product_facts]
        identities = {line.product_id: line.canonical_url for line in self.lines}
        availability: dict[str, str] = {}
        for fact in facts:
            if (
                fact.product_id in availability
                and availability[fact.product_id] != fact.availability
            ):
                raise ValueError("contradictory current product availability")
            availability[fact.product_id] = fact.availability
            if fact.product_id in identities and identities[fact.product_id] != fact.canonical_url:
                raise ValueError("contradictory current product identity")
            identities[fact.product_id] = fact.canonical_url
            if fact.displayed_price_eur is None:
                continue
            if fact.product_id in prices and prices[fact.product_id] != Decimal(
                fact.displayed_price_eur
            ):
                raise ValueError("contradictory current cart and product fact prices")
            prices[fact.product_id] = Decimal(fact.displayed_price_eur)
        return self

    @field_validator("recurring_product_facts", "recipe_product_facts")
    @classmethod
    def _check_facts(cls, value: list[RecurringProductFact]) -> list[RecurringProductFact]:
        if len({f.product_id for f in value}) != len(value):
            raise ValueError("duplicate current product identity")
        for fact in value:
            if product_id_from_url(fact.canonical_url) != fact.product_id:
                raise ValueError("current product URL and identity disagree")
        cls._check_lines(
            [
                CartObservationLine.model_validate(
                    {
                        "product_id": fact.product_id,
                        "canonical_url": fact.canonical_url,
                        "displayed_price_eur": fact.displayed_price_eur,
                        "quantity": 1,
                    }
                )
                for fact in value
                if fact.displayed_price_eur is not None
            ]
        )
        for fact in value:
            if fact.displayed_price_eur is not None and not re.fullmatch(
                r"(?:0|[1-9][0-9]*)\.[0-9]{2}", fact.displayed_price_eur
            ):
                raise ValueError("current product price must have exactly two decimal places")
        return value

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
        seen_ids: set[str] = set()
        for line in v:
            url_id = product_id_from_url(line.canonical_url)
            if url_id is not None and url_id != line.product_id:
                raise ValueError(
                    f"product_id {line.product_id!r} disagrees with the ID "
                    f"carried by canonical_url ({url_id!r})"
                )
            if line.product_id in seen_ids:
                raise ValueError(
                    f"duplicate product_id {line.product_id!r} in observation lines; "
                    "each stable product identity must appear at most once"
                )
            seen_ids.add(line.product_id)
        return v


def _scan_prohibited(obj: Any) -> None:
    """Recursively scan all string values for prohibited content patterns."""
    try:
        scan_prohibited_content(obj)
    except ProhibitedContentError as exc:
        raise CartObservationError("prohibited content in observation") from exc


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
    except ValidationError as exc:
        first = exc.errors(include_input=False)[0]
        location = ".".join(str(part) for part in first["loc"])
        raise CartObservationError(
            f"cart observation validation failed at {location}: {first['msg']}"
        ) from exc
