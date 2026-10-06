"""Tests for the versioned cart observation envelope (PRD section 11, family 5).

Verifies that the closed schema rejects unknown fields, invalid product IDs,
invalid URLs, non-positive quantities, invalid prices, prohibited content,
and schema mismatches. All tests use in-memory data; no network, browser,
or live cart access.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from grossy.browser.observation import (
    CART_OBSERVATION_SCHEMA_NAME,
    CartObservationError,
    RecipeProductFact,
    RecurringProductFact,
    validate_cart_observation,
)

VALID_URL = "https://www.ab.gr/en/eshop/Dairy/Yogurt/p/1234567"
VALID_URL_2 = "https://www.ab.gr/el/eshop/Βουτυρό/Γαλακτοκομικά/p/7654321"


def _valid_data(**overrides: object) -> dict:
    base: dict = {
        "schema_name": CART_OBSERVATION_SCHEMA_NAME,
        "schema_version": 1,
        "generated_at": "2026-09-26T12:00:00Z",
        "lines": [
            {
                "product_id": "1234567",
                "canonical_url": VALID_URL,
                "quantity": 2,
                "displayed_price_eur": "12.99",
            }
        ],
    }
    base.update(overrides)
    return base


class TestValidObservation:
    def test_minimal_valid(self) -> None:
        result = validate_cart_observation(_valid_data())
        assert result.schema_name == CART_OBSERVATION_SCHEMA_NAME
        assert result.schema_version == 1
        assert result.generated_at == "2026-09-26T12:00:00Z"
        assert len(result.lines) == 1
        assert result.lines[0].product_id == "1234567"
        assert result.lines[0].quantity == 2
        assert result.lines[0].displayed_price_eur == "12.99"

    def test_empty_lines(self) -> None:
        result = validate_cart_observation(_valid_data(lines=[]))
        assert result.lines == []

    def test_multiple_lines_with_el_locale(self) -> None:
        data = _valid_data(
            lines=[
                {
                    "product_id": "1234567",
                    "canonical_url": VALID_URL,
                    "quantity": 1,
                    "displayed_price_eur": "3.49",
                },
                {
                    "product_id": "7654321",
                    "canonical_url": VALID_URL_2,
                    "quantity": 3,
                    "displayed_price_eur": "5.99",
                },
            ]
        )
        result = validate_cart_observation(data)
        assert [line.product_id for line in result.lines] == ["1234567", "7654321"]

    @pytest.mark.parametrize(
        "field,value",
        [
            pytest.param("displayed_price_eur", "1299", id="integer-minor-units-price"),
            pytest.param("generated_at", "2026-09-26T15:00:00+03:00", id="offset-timestamp"),
        ],
    )
    def test_accepted_scalar_forms(self, field: str, value: str) -> None:
        data = _valid_data()
        target = data if field == "generated_at" else data["lines"][0]
        target[field] = value
        result = validate_cart_observation(data)
        actual = (
            result.generated_at if field == "generated_at" else result.lines[0].displayed_price_eur
        )
        assert actual == value


@pytest.mark.parametrize(
    "overrides,match",
    [
        pytest.param({"schema_name": "grossy.cart.observation.v2"}, "schema_name", id="wrong-name"),
        pytest.param({"schema_version": 2}, "schema_version", id="wrong-version"),
        pytest.param({"schema_version": "1"}, "schema_version", id="string-typed-version"),
        pytest.param({"extra_field": "value"}, None, id="unknown-top-level-field"),
        pytest.param({"generated_at": None}, None, id="missing-generated_at"),
        pytest.param({"lines": None}, None, id="missing-lines"),
        pytest.param({"generated_at": "not-a-timestamp"}, "ISO 8601", id="invalid-timestamp"),
        pytest.param({"generated_at": ""}, "ISO 8601", id="empty-timestamp"),
        # PRD 11: generated_at must be a timezone-aware instant, not a date.
        pytest.param({"generated_at": "2026-09-26"}, "timezone-aware", id="date-only"),
        pytest.param({"generated_at": "2026-09-26T12:00:00"}, "timezone-aware", id="naive"),
    ],
)
def test_envelope_violations_rejected(overrides: dict[str, Any], match: str | None) -> None:
    data = _valid_data()
    for key, value in overrides.items():
        if value is None:
            del data[key]
        else:
            data[key] = value
    with pytest.raises(CartObservationError, match=match):
        validate_cart_observation(data)


@pytest.mark.parametrize(
    "field,value,match",
    [
        pytest.param("unknown", "x", None, id="unknown-line-field"),
        pytest.param("product_id", "abc123", None, id="non-numeric-product-id"),
        pytest.param("product_id", "", None, id="empty-product-id"),
        pytest.param("product_id", "9999999", "disagrees", id="product-id-mismatches-url"),
        pytest.param("product_id", "SELECT * FROM users", None, id="sql-in-product-id"),
        pytest.param(
            "canonical_url",
            "https://www.other.com/en/eshop/Dairy/Yogurt/p/1234567",
            None,
            id="wrong-host",
        ),
        pytest.param(
            "canonical_url",
            "http://www.ab.gr/en/eshop/Dairy/Yogurt/p/1234567",
            None,
            id="non-https",
        ),
        pytest.param(
            "canonical_url", "https://www.ab.gr/other/Dairy/Yogurt/p/1234567", None, id="non-eshop"
        ),
        # PRD 17.4: percent-encoded markup must not bypass the URL identity gate.
        pytest.param(
            "canonical_url",
            "https://www.ab.gr/en/eshop/Dairy/%3Cscript%3Ealert(1)%3Cscript%3E/p/1234567",
            None,
            id="encoded-script-tag",
        ),
        pytest.param(
            "canonical_url",
            "https://www.ab.gr/en/eshop/Dairy/%3Ctest%3E/p/1234567",
            None,
            id="encoded-angle-brackets",
        ),
        pytest.param(
            "canonical_url",
            "https://www.ab.gr/en/eshop/Dairy/item%3E/p/1234567",
            None,
            id="encoded-greater-than",
        ),
        pytest.param("canonical_url", "<script>alert('xss')</script>", "prohibited", id="script"),
        pytest.param("canonical_url", "javascript:alert(1)", "prohibited", id="javascript-uri"),
        pytest.param("canonical_url", "DROP TABLE products", "prohibited", id="drop-table"),
        pytest.param("quantity", 0, None, id="zero-quantity"),
        pytest.param("quantity", -1, None, id="negative-quantity"),
        pytest.param("quantity", "2", "quantity", id="string-typed-quantity"),
        pytest.param("quantity", 2.0, "quantity", id="float-typed-quantity"),
        pytest.param("displayed_price_eur", "0", "positive", id="zero-price"),
        pytest.param("displayed_price_eur", "-5.00", "positive", id="negative-price"),
        pytest.param("displayed_price_eur", "abc", "decimal", id="non-numeric-price"),
        pytest.param("displayed_price_eur", 12.99, None, id="binary-float-price"),
        pytest.param("displayed_price_eur", "Infinity", "finite", id="infinity-price"),
        pytest.param("displayed_price_eur", "NaN", "finite", id="nan-price"),
        pytest.param(
            "displayed_price_eur", "data:image/png;base64,abc123", "prohibited", id="base64-image"
        ),
        pytest.param(
            "displayed_price_eur", "SET-COOKIE: session=abc", "prohibited", id="set-cookie"
        ),
    ],
)
def test_line_field_violations_rejected(field: str, value: Any, match: str | None) -> None:
    data = _valid_data()
    data["lines"][0][field] = value
    with pytest.raises(CartObservationError, match=match):
        validate_cart_observation(data)


@pytest.mark.parametrize("fact_field", ["recurring_product_facts", "recipe_product_facts"])
@pytest.mark.parametrize("price", ["not-a-price", "NaN", "sNaN", "Infinity", "0.00", "-1.00"])
def test_current_fact_prices_fail_controlled_validation_without_echo(
    fact_field: str, price: str
) -> None:
    fact = {
        "product_id": "1234567",
        "canonical_url": VALID_URL,
        "displayed_price_eur": price,
        "availability": "available",
    }
    if fact_field == "recipe_product_facts":
        fact.update(package_amount="1", package_unit="piece", package_count=1, sale_kind="packaged")
    model = RecipeProductFact if fact_field == "recipe_product_facts" else RecurringProductFact
    with pytest.raises(ValidationError) as model_error:
        model.model_validate(fact)
    assert model_error.value.errors(include_input=False)[0]["loc"] == ("displayed_price_eur",)
    with pytest.raises(CartObservationError) as error:
        validate_cart_observation(_valid_data(**{fact_field: [fact]}))
    assert f"{fact_field}.0.displayed_price_eur" in str(error.value)
    assert price not in str(error.value)
