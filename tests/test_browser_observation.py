"""Tests for the versioned cart observation envelope (PRD section 11, family 5).

Verifies that the closed schema rejects unknown fields, invalid product IDs,
invalid URLs, non-positive quantities, invalid prices, prohibited content,
and schema mismatches. All tests use in-memory data; no network, browser,
or live cart access.
"""

from __future__ import annotations

import pytest

from grossy.browser.observation import (
    CART_OBSERVATION_SCHEMA_NAME,
    CartObservation,
    CartObservationError,
    CartObservationLine,
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
        assert len(result.lines) == 1
        assert result.lines[0].product_id == "1234567"
        assert result.lines[0].quantity == 2
        assert result.lines[0].displayed_price_eur == "12.99"

    def test_empty_lines(self) -> None:
        result = validate_cart_observation(_valid_data(lines=[]))
        assert result.lines == []

    def test_multiple_lines(self) -> None:
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
        assert len(result.lines) == 2

    def test_integer_minor_units_price(self) -> None:
        # "1299" is a valid decimal string (integer minor units)
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "1299"
        result = validate_cart_observation(data)
        assert result.lines[0].displayed_price_eur == "1299"


class TestSchemaRejection:
    def test_wrong_schema_name(self) -> None:
        with pytest.raises(CartObservationError, match="schema_name"):
            validate_cart_observation(_valid_data(schema_name="grossy.cart.observation.v2"))

    def test_wrong_schema_version(self) -> None:
        with pytest.raises(CartObservationError, match="schema_version"):
            validate_cart_observation(_valid_data(schema_version=2))

    def test_unknown_top_level_field(self) -> None:
        with pytest.raises(CartObservationError):
            validate_cart_observation(_valid_data(extra_field="value"))

    def test_unknown_line_field(self) -> None:
        data = _valid_data()
        data["lines"][0]["unknown"] = "x"
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_missing_generated_at(self) -> None:
        data = _valid_data()
        del data["generated_at"]
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_missing_lines(self) -> None:
        data = _valid_data()
        del data["lines"]
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)


class TestProductIdValidation:
    def test_non_numeric_product_id(self) -> None:
        data = _valid_data()
        data["lines"][0]["product_id"] = "abc123"
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_empty_product_id(self) -> None:
        data = _valid_data()
        data["lines"][0]["product_id"] = ""
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_product_id_mismatches_url(self) -> None:
        data = _valid_data()
        data["lines"][0]["product_id"] = "9999999"
        with pytest.raises(CartObservationError, match="disagrees"):
            validate_cart_observation(data)


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("https://www.other.com/en/eshop/Dairy/Yogurt/p/1234567", id="wrong-host"),
            pytest.param("http://www.ab.gr/en/eshop/Dairy/Yogurt/p/1234567", id="non-https"),
            pytest.param("https://www.ab.gr/other/Dairy/Yogurt/p/1234567", id="non-eshop-route"),
        ],
    )
    def test_rejects_noncanonical_product_url(self, url: str) -> None:
        data = _valid_data()
        data["lines"][0]["canonical_url"] = url
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_el_locale_valid(self) -> None:
        data = _valid_data()
        data["lines"][0]["product_id"] = "7654321"
        data["lines"][0]["canonical_url"] = VALID_URL_2
        result = validate_cart_observation(data)
        assert result.lines[0].product_id == "7654321"


class TestQuantityValidation:
    def test_zero_quantity(self) -> None:
        data = _valid_data()
        data["lines"][0]["quantity"] = 0
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_negative_quantity(self) -> None:
        data = _valid_data()
        data["lines"][0]["quantity"] = -1
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)


class TestPriceValidation:
    def test_zero_price(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "0"
        with pytest.raises(CartObservationError, match="positive"):
            validate_cart_observation(data)

    def test_negative_price(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "-5.00"
        with pytest.raises(CartObservationError, match="positive"):
            validate_cart_observation(data)

    def test_non_numeric_price(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "abc"
        with pytest.raises(CartObservationError, match="decimal"):
            validate_cart_observation(data)

    def test_float_string_rejected_as_float_type(self) -> None:
        # Binary float is not a valid decimal string representation
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = 12.99  # type: ignore[dict-item]
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_infinity_price_rejected(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "Infinity"
        with pytest.raises(CartObservationError, match="finite"):
            validate_cart_observation(data)

    def test_nan_price_rejected(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "NaN"
        with pytest.raises(CartObservationError, match="finite"):
            validate_cart_observation(data)


class TestStrictIntegerTypes:
    """Integer fields must reject string-typed values (no lax coercion)."""

    def test_string_typed_schema_version_rejected(self) -> None:
        with pytest.raises(CartObservationError, match="schema_version"):
            validate_cart_observation(_valid_data(schema_version="1"))

    def test_string_typed_quantity_rejected(self) -> None:
        data = _valid_data()
        data["lines"][0]["quantity"] = "2"  # type: ignore[literal-required]
        with pytest.raises(CartObservationError, match="quantity"):
            validate_cart_observation(data)

    def test_float_typed_quantity_rejected(self) -> None:
        data = _valid_data()
        data["lines"][0]["quantity"] = 2.0  # type: ignore[dict-item]
        with pytest.raises(CartObservationError, match="quantity"):
            validate_cart_observation(data)


class TestTimestampValidation:
    def test_valid_iso_8601_z(self) -> None:
        result = validate_cart_observation(_valid_data())
        assert result.generated_at == "2026-09-26T12:00:00Z"

    def test_valid_iso_8601_offset(self) -> None:
        data = _valid_data(generated_at="2026-09-26T15:00:00+03:00")
        result = validate_cart_observation(data)
        assert result.generated_at == "2026-09-26T15:00:00+03:00"

    def test_invalid_timestamp(self) -> None:
        data = _valid_data(generated_at="not-a-timestamp")
        with pytest.raises(CartObservationError, match="ISO 8601"):
            validate_cart_observation(data)

    def test_empty_timestamp(self) -> None:
        data = _valid_data(generated_at="")
        with pytest.raises(CartObservationError, match="ISO 8601"):
            validate_cart_observation(data)

    def test_date_only_rejected(self) -> None:
        """PRD 11: generated_at must be a timestamp (instant), not a date."""
        data = _valid_data(generated_at="2026-09-26")
        with pytest.raises(CartObservationError, match="timezone-aware"):
            validate_cart_observation(data)

    def test_naive_timestamp_rejected(self) -> None:
        """PRD 12.1: timestamps must be UTC instants (timezone required)."""
        data = _valid_data(generated_at="2026-09-26T12:00:00")
        with pytest.raises(CartObservationError, match="timezone-aware"):
            validate_cart_observation(data)


class TestEncodedContentBypass:
    """Percent-encoded executable markup must not bypass the safety boundary.

    PRD 17.4: browser and recipe content must be treated as untrusted data
    and never executable instructions. The URL identity gate must reject
    decoded angle brackets so that ``%3Cscript%3E`` cannot pass validation.
    """

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param(
                "https://www.ab.gr/en/eshop/Dairy/%3Cscript%3Ealert(1)%3Cscript%3E/p/1234567",
                id="encoded-script-tag",
            ),
            pytest.param(
                "https://www.ab.gr/en/eshop/Dairy/%3Ctest%3E/p/1234567",
                id="encoded-angle-brackets",
            ),
            pytest.param(
                "https://www.ab.gr/en/eshop/Dairy/item%3E/p/1234567",
                id="encoded-greater-than-only",
            ),
        ],
    )
    def test_rejects_encoded_markup_in_product_url(self, url: str) -> None:
        data = _valid_data()
        data["lines"][0]["canonical_url"] = url
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)


class TestProhibitedContent:
    """Tests that reject untrusted executable content in observation strings."""

    def test_script_tag_in_url(self) -> None:
        data = _valid_data()
        data["lines"][0]["canonical_url"] = "<script>alert('xss')</script>"
        with pytest.raises(CartObservationError, match="prohibited"):
            validate_cart_observation(data)

    def test_sql_in_product_id_context(self) -> None:
        data = _valid_data()
        data["lines"][0]["product_id"] = "SELECT * FROM users"
        with pytest.raises(CartObservationError):
            validate_cart_observation(data)

    def test_javascript_uri(self) -> None:
        data = _valid_data()
        data["lines"][0]["canonical_url"] = "javascript:alert(1)"
        with pytest.raises(CartObservationError, match="prohibited"):
            validate_cart_observation(data)

    def test_base64_image(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "data:image/png;base64,abc123"
        with pytest.raises(CartObservationError, match="prohibited"):
            validate_cart_observation(data)

    def test_set_cookie_header(self) -> None:
        data = _valid_data()
        data["lines"][0]["displayed_price_eur"] = "SET-COOKIE: session=abc"
        with pytest.raises(CartObservationError, match="prohibited"):
            validate_cart_observation(data)

    def test_drop_table(self) -> None:
        data = _valid_data()
        data["lines"][0]["canonical_url"] = "DROP TABLE products"
        with pytest.raises(CartObservationError, match="prohibited"):
            validate_cart_observation(data)


class TestPydanticModelDirect:
    """Test the Pydantic model directly (not through validate_cart_observation)."""

    def test_line_model_valid(self) -> None:
        line = CartObservationLine(
            product_id="1234567",
            canonical_url=VALID_URL,
            quantity=1,
            displayed_price_eur="9.99",
        )
        assert line.product_id == "1234567"

    def test_observation_model_valid(self) -> None:
        obs = CartObservation(
            schema_name=CART_OBSERVATION_SCHEMA_NAME,
            schema_version=1,
            generated_at="2026-09-26T12:00:00Z",
            lines=[
                CartObservationLine(
                    product_id="1234567",
                    canonical_url=VALID_URL,
                    quantity=1,
                    displayed_price_eur="9.99",
                )
            ],
        )
        assert len(obs.lines) == 1
