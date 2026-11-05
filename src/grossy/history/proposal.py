"""Versioned, schema-bound history synchronization proposals (PRD 11, FR-8..FR-24).

Two proposal families cross the browser/agent boundary into the deterministic
CLI:

- ``grossy.history.listed-orders.v2``: every order enumerated from the
  reservation-history list in the current walk (any count, including zero),
  each with its numeric-string ID, ISO list date and order-details URL.
- ``grossy.history.order-scan.v1``: the stabilized product scan of every
  unknown order in one synchronization.

Both are closed schemas (unknown fields are rejected), use numeric-string
stable IDs, ISO 8601 instants and ISO ``YYYY-MM-DD`` dates, and are scanned for
prohibited content before any field is trusted.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from grossy.config.models import (
    NUMERIC_ID_PATTERN,
    PRODUCT_URL_HOST,
    product_id_from_url,
)
from grossy.security import ProhibitedContentError, scan_prohibited_content

LISTED_ORDERS_SCHEMA_NAME = "grossy.history.listed-orders.v2"
LISTED_ORDERS_SCHEMA_VERSION = 2

ORDER_SCAN_SCHEMA_NAME = "grossy.history.order-scan.v1"
ORDER_SCAN_SCHEMA_VERSION = 1

# Deterministic internal proposal family for high-confidence automatic family
# links (FR-55, FR-56). The inference pass produces it, it is validated
# fail-closed, and only the validated links may change persisted family
# membership. Explicit user links arrive through task-06 correction commands,
# not through this proposal.
FAMILY_LINKS_SCHEMA_NAME = "grossy.history.family-links.v1"
FAMILY_LINKS_SCHEMA_VERSION = 1

# The only order navigation route, shared by listed-orders v2 and order-scan
# v1: the AB.gr order-details page selected by exactly one raw numeric ``id``
# query.
ORDER_DETAILS_PATH = "/my-account/order"
_ORDER_DETAILS_QUERY_RE = re.compile(r"^id=[0-9]+$")

# Displayed label form DD/MM/YYYY (FR-8).
_DISPLAYED_LABEL_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
# Normalized ISO date YYYY-MM-DD (PRD 11). ``date.fromisoformat`` accepts the
# looser ``YYYYMMDD`` form on Python 3.11+, so the regex gate is required.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HistoryProposalError(ValueError):
    """Raised when a history proposal fails deterministic validation."""


# --- shared field helpers ---------------------------------------------------


def _parse_displayed_label(label: str) -> date:
    match = _DISPLAYED_LABEL_RE.fullmatch(label)
    if not match:
        raise HistoryProposalError(f"displayed_label {label!r} must be in DD/MM/YYYY form")
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise HistoryProposalError(
            f"displayed_label {label!r} is not a valid calendar date"
        ) from None


def _parse_iso_date(value: str) -> date:
    if not _ISO_DATE_RE.fullmatch(value):
        raise HistoryProposalError(f"order_date {value!r} must be an ISO YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HistoryProposalError(f"order_date {value!r} is not a valid calendar date") from None


def _check_generated_at(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HistoryProposalError(
            f"generated_at must be a valid ISO 8601 timestamp, got {value!r}"
        ) from None
    if parsed.tzinfo is None:
        raise HistoryProposalError(
            f"generated_at must be a timezone-aware ISO 8601 instant, got {value!r}"
        )


def _is_safe_url_char(char: str) -> bool:
    return char.isprintable() and char not in (" ", "<", ">")


def _validate_order_details_url(url: str) -> None:
    """Validate an order-details navigation URL (listed-orders v2, order-scan v1).

    The only accepted form is
    ``https://www.ab.gr/my-account/order?id=<numeric-string>``: the canonical
    https authority, the literal order-details path (no language prefix, no
    trailing slash, no percent-encoding) and exactly one raw numeric ``id``
    query. The model validators bind that query to the source ID. Fragments,
    extra queries and unsafe characters are rejected.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise HistoryProposalError("navigation_url is malformed") from None
    if parsed.scheme != "https" or parsed.netloc != PRODUCT_URL_HOST:
        raise HistoryProposalError(
            f"navigation_url must be an https URL on the canonical {PRODUCT_URL_HOST} host"
        )
    if "#" in url:
        raise HistoryProposalError("navigation_url must not contain a fragment")
    if "\\" in url or any(not _is_safe_url_char(char) for char in url):
        raise HistoryProposalError("navigation_url contains unsafe characters")
    if parsed.path != ORDER_DETAILS_PATH or not _ORDER_DETAILS_QUERY_RE.fullmatch(parsed.query):
        raise HistoryProposalError(
            "navigation_url must be the order-details route "
            f"https://{PRODUCT_URL_HOST}{ORDER_DETAILS_PATH}?id=<numeric-string>"
        )


def _check_order_details_identity(url: str, source_order_id: str) -> None:
    """Bind the validated order-details ``id`` query to its exact source ID.

    IDs are compared as strings so leading zeros are preserved.
    """
    if urlsplit(url).query.removeprefix("id=") != source_order_id:
        raise HistoryProposalError("navigation_url id must match source_order_id")


def _format_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid"))
        return f"{location}: {message}" if location else message
    return str(exc)


# --- listed-orders proposal (grossy.history.listed-orders.v2) ---------------


class ListedOrder(BaseModel):
    """One order enumerated from the reservation-history list.

    ``order_date`` is the list entry's date normalized to ISO ``YYYY-MM-DD``
    by the skill script (strict day-first parse of the rendered
    ``DD/MM/YYYY``); it is the only date field.
    """

    model_config = ConfigDict(extra="forbid")

    source_order_id: str = Field(pattern=NUMERIC_ID_PATTERN)
    order_date: str
    navigation_url: str

    @field_validator("order_date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _parse_iso_date(value)
        return value

    @field_validator("navigation_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        _validate_order_details_url(value)
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> ListedOrder:
        _check_order_details_identity(self.navigation_url, self.source_order_id)
        return self


class ListedOrdersProposal(BaseModel):
    """Versioned listed-orders proposal: any order count, including zero."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    schema_version: StrictInt
    generated_at: str
    orders: list[ListedOrder]

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str) -> str:
        if value != LISTED_ORDERS_SCHEMA_NAME:
            raise HistoryProposalError(
                f"schema_name must be {LISTED_ORDERS_SCHEMA_NAME!r}, got {value!r}"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != LISTED_ORDERS_SCHEMA_VERSION:
            raise HistoryProposalError(
                f"schema_version must be {LISTED_ORDERS_SCHEMA_VERSION}, got {value}"
            )
        return value

    @field_validator("generated_at")
    @classmethod
    def _check_generated_at(cls, value: str) -> str:
        _check_generated_at(value)
        return value

    @model_validator(mode="after")
    def _check_unique_ids(self) -> ListedOrdersProposal:
        seen: set[str] = set()
        for order in self.orders:
            if order.source_order_id in seen:
                raise HistoryProposalError("duplicate source order ID")
            seen.add(order.source_order_id)
        return self


# --- order-scan proposal (grossy.history.order-scan.v1) ---------------------


class ScannedProduct(BaseModel):
    """One stabilized product card from an order scan (FR-17, FR-18, FR-19).

    An order-product record is binary presence: there is no quantity field.
    Package and measure facts and the displayed price are optional diagnostics.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern=NUMERIC_ID_PATTERN)
    canonical_url: str
    title: str = Field(min_length=1)
    brand: str | None = None
    category_heading: str | None = None
    package_amount: StrictFloat | None = None
    package_unit: str | None = None
    package_count: StrictInt | None = None
    package_display: str | None = None
    displayed_price_eur: str | None = None

    @field_validator("canonical_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if product_id_from_url(value) is None:
            raise HistoryProposalError(
                "canonical_url must be a supported AB.gr product URL of the form "
                "https://www.ab.gr/[en|el/]eshop/.../p/<id>"
            )
        return value

    @field_validator("displayed_price_eur")
    @classmethod
    def _check_price(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise HistoryProposalError(
                f"displayed_price_eur must be a decimal string, got {value!r}"
            ) from None
        if not parsed.is_finite() or parsed <= 0:
            raise HistoryProposalError(
                f"displayed_price_eur must be a positive finite decimal, got {value!r}"
            )
        return value

    @field_validator("package_amount")
    @classmethod
    def _check_package_amount(cls, value: float | None) -> float | None:
        if value is None:
            return value
        if not math.isfinite(value):
            raise HistoryProposalError(f"package_amount must be a finite number, got {value!r}")
        if value <= 0:
            raise HistoryProposalError(f"package_amount must be positive, got {value!r}")
        return value

    @field_validator("package_count")
    @classmethod
    def _check_package_count(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value <= 0:
            raise HistoryProposalError(f"package_count must be positive, got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_identity(self) -> ScannedProduct:
        url_id = product_id_from_url(self.canonical_url)
        if url_id is not None and url_id != self.product_id:
            raise HistoryProposalError(
                f"product_id {self.product_id!r} disagrees with the ID carried "
                f"by canonical_url ({url_id!r})"
            )
        return self


class ScannedOrder(BaseModel):
    """One unknown order's stabilized scan (FR-15..FR-21)."""

    model_config = ConfigDict(extra="forbid")

    source_order_id: str = Field(pattern=NUMERIC_ID_PATTERN)
    displayed_label: str
    order_date: str
    navigation_url: str
    # Diagnostics only (FR-20): the rendered displayed count, never the import
    # basis. The import uses the stabilized unique product IDs.
    displayed_product_count: StrictInt | None = None
    products: list[ScannedProduct]

    @field_validator("displayed_label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        _parse_displayed_label(value)
        return value

    @field_validator("order_date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _parse_iso_date(value)
        return value

    @field_validator("navigation_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        _validate_order_details_url(value)
        return value

    @field_validator("displayed_product_count")
    @classmethod
    def _check_count(cls, value: StrictInt | None) -> StrictInt | None:
        if value is not None and value < 0:
            raise HistoryProposalError(f"displayed_product_count must be non-negative, got {value}")
        return value

    @model_validator(mode="after")
    def _check_order(self) -> ScannedOrder:
        _check_order_details_identity(self.navigation_url, self.source_order_id)
        if _parse_displayed_label(self.displayed_label) != _parse_iso_date(self.order_date):
            raise HistoryProposalError(
                "source order: displayed_label "
                f"{self.displayed_label!r} disagrees with order_date {self.order_date!r}"
            )
        if not self.products:
            raise HistoryProposalError(
                "source order has no product cards; an order scan must contain at least one product"
            )
        seen: set[str] = set()
        for product in self.products:
            if product.product_id in seen:
                raise HistoryProposalError(
                    f"source order: duplicate product ID {product.product_id!r}"
                )
            seen.add(product.product_id)
        return self


class OrderScanProposal(BaseModel):
    """Versioned order-scan proposal containing all scanned unknown orders (FR-23)."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    schema_version: StrictInt
    generated_at: str
    orders: list[ScannedOrder]

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str) -> str:
        if value != ORDER_SCAN_SCHEMA_NAME:
            raise HistoryProposalError(
                f"schema_name must be {ORDER_SCAN_SCHEMA_NAME!r}, got {value!r}"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != ORDER_SCAN_SCHEMA_VERSION:
            raise HistoryProposalError(
                f"schema_version must be {ORDER_SCAN_SCHEMA_VERSION}, got {value}"
            )
        return value

    @field_validator("generated_at")
    @classmethod
    def _check_generated_at(cls, value: str) -> str:
        _check_generated_at(value)
        return value

    @model_validator(mode="after")
    def _check_scan(self) -> OrderScanProposal:
        if not self.orders:
            raise HistoryProposalError("an order-scan proposal must contain at least one order")
        seen: set[str] = set()
        for order in self.orders:
            if order.source_order_id in seen:
                raise HistoryProposalError("duplicate source order ID")
            seen.add(order.source_order_id)
        return self


# --- family-links proposal (grossy.history.family-links.v1) -----------------


class FamilyLink(BaseModel):
    """One high-confidence automatic family link (FR-55).

    Only ``high``-confidence links may enter this proposal: lower-confidence
    candidates must remain review items with the products left separate
    (FR-56). ``provenance`` records where the link came from; the inference
    pass emits deterministic ``inferred`` links only.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern=NUMERIC_ID_PATTERN)
    target_family_id: str = Field(min_length=1)
    base_name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    confidence: Literal["high"]
    provenance: Literal["inferred"]


class FamilyLinksProposal(BaseModel):
    """Versioned, schema-bound proposal of automatic family links (FR-55)."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    schema_version: StrictInt
    generated_at: str
    links: list[FamilyLink]

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str) -> str:
        if value != FAMILY_LINKS_SCHEMA_NAME:
            raise HistoryProposalError(
                f"schema_name must be {FAMILY_LINKS_SCHEMA_NAME!r}, got {value!r}"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != FAMILY_LINKS_SCHEMA_VERSION:
            raise HistoryProposalError(
                f"schema_version must be {FAMILY_LINKS_SCHEMA_VERSION}, got {value}"
            )
        return value

    @field_validator("generated_at")
    @classmethod
    def _check_generated_at(cls, value: str) -> str:
        _check_generated_at(value)
        return value

    @model_validator(mode="after")
    def _check_links(self) -> FamilyLinksProposal:
        seen: set[str] = set()
        for link in self.links:
            if link.product_id in seen:
                raise HistoryProposalError(
                    f"duplicate family link for product {link.product_id!r}; a "
                    "product may be linked at most once per pass"
                )
            seen.add(link.product_id)
        return self


# --- public validators (fail closed) ----------------------------------------


def validate_listed_orders(data: dict[str, Any]) -> ListedOrdersProposal:
    """Validate a raw listed-orders proposal dict; fail closed on any violation."""
    try:
        scan_prohibited_content(data)
    except ProhibitedContentError as exc:
        raise HistoryProposalError(str(exc)) from exc
    try:
        return ListedOrdersProposal.model_validate(data)
    except ValidationError as exc:
        raise HistoryProposalError(
            f"listed-orders proposal validation failed: {_format_validation_error(exc)}"
        ) from exc


def validate_order_scan(data: dict[str, Any]) -> OrderScanProposal:
    """Validate a raw order-scan proposal dict; fail closed on any violation."""
    try:
        scan_prohibited_content(data)
    except ProhibitedContentError as exc:
        raise HistoryProposalError(str(exc)) from exc
    try:
        return OrderScanProposal.model_validate(data)
    except ValidationError as exc:
        raise HistoryProposalError(
            f"order-scan proposal validation failed: {_format_validation_error(exc)}"
        ) from exc


def validate_family_links(data: dict[str, Any]) -> FamilyLinksProposal:
    """Validate a raw family-links proposal dict; fail closed on any violation.

    This is the validated-proposal boundary for automatic family links
    (FR-55): a proposal that does not validate here must not change any
    persisted family membership.
    """
    try:
        scan_prohibited_content(data)
    except ProhibitedContentError as exc:
        raise HistoryProposalError(str(exc)) from exc
    try:
        return FamilyLinksProposal.model_validate(data)
    except ValidationError as exc:
        raise HistoryProposalError(
            f"family-links proposal validation failed: {_format_validation_error(exc)}"
        ) from exc
