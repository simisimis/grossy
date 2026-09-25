"""Tests for strict YAML configuration validation (PRD sections 7.3, 18, 19.1)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from grossy.config import (
    GrossyConfigError,
    StapleModel,
    load_config_file,
    load_config_text,
    product_id_from_url,
)

# Real observed AB.gr product page URL (feasibility task 09 evidence).
OBSERVED_PRODUCT_URL = (
    "https://www.ab.gr/en/eshop/Dairy-Plant-Based-Beverages-and-Chilled-Products/"
    "Yogurt/Plain-yoghurts/Yogurt-Dessert-2-Fat-1kg/p/7633916"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONFIG = REPO_ROOT / "config" / "grossy.sample.yaml"


def config_text(
    schema_version: str = "1",
    retailer: str = "ab",
    timezone: str = "Europe/Athens",
    currency: str = "EUR",
    household: str = "  adults: 2\n  children: 1\n  recipe_servings: 3",
    appliances: str = (
        "  thermomix:\n    available: true\n"
        "  dough_kneading_machine:\n    available: true\n"
        "  steam_oven:\n    available: true\n"
        "  air_fryer:\n    available: true\n"
        "  induction_cooktop:\n    available: true\n"
        "  microwave:\n    available: true\n"
        "  pressure_cooker:\n    available: true\n"
        "  dutch_oven:\n    available: true"
    ),
    appliance_preferences: str = (
        "  prefer_air_fryer_over_pan_frying: true\n  prefer_thermomix_native_over_manual: true"
    ),
    staples: str = "  breakfast: []\n  lunch: []",
    weekly_budget: str = "100",
    material_unit_price_improvement_pct: str = "15",
) -> str:
    """Build a minimal valid configuration text with per-field overrides."""
    return f"""\
schema_version: {schema_version}
retailer: {retailer}
timezone: {timezone}
currency: {currency}

household:
{household}

weekly_budget: {weekly_budget}
material_unit_price_improvement_pct: {material_unit_price_improvement_pct}

disliked_ingredients: []
pantry_assumptions: []

appliances:
{appliances}

appliance_preferences:
{appliance_preferences}

staples:
{staples}
"""


def staples_text(breakfast: str, lunch: str = "  lunch: []") -> str:
    return f"  breakfast:\n{breakfast}\n{lunch}" if breakfast else "  breakfast: []\n  lunch: []"


class TestCanonicalSample:
    def test_sample_validates(self) -> None:
        load_config_file(SAMPLE_CONFIG)

    def test_sample_fields(self) -> None:
        config = load_config_file(SAMPLE_CONFIG)
        assert config.schema_version == 1
        assert config.retailer == "ab"
        assert config.timezone == "Europe/Athens"
        assert config.currency == "EUR"
        assert config.household.adults == 2
        assert config.household.children == 1
        assert config.household.recipe_servings == 3
        assert config.weekly_budget == Decimal("100")
        assert config.material_unit_price_improvement_pct == Decimal("15")
        assert config.pantry_assumptions == ["salt", "black pepper", "olive oil"]
        assert config.appliances.thermomix.available is True
        assert config.appliances.air_fryer.capacity_litres is None
        assert config.staples.breakfast == []
        assert config.staples.lunch == []

    def test_defaults_apply_when_omitted(self) -> None:
        text = (
            config_text()
            .replace("weekly_budget: 100\n", "")
            .replace("material_unit_price_improvement_pct: 15\n", "")
        )
        config = load_config_text(text)
        assert config.weekly_budget == Decimal("100")
        assert config.material_unit_price_improvement_pct == Decimal("15")


class TestExactLiterals:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("schema_version", "2"),
            ("retailer", "carrefour"),
            ("timezone", "UTC"),
            ("currency", "USD"),
        ],
    )
    def test_invalid_literal_fails_with_path(self, field: str, value: str) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text(**{field: value}))
        assert f"config.{field}:" in str(excinfo.value)


class TestSchemaVersionStrictness:
    """schema_version must be the strict integer 1 (PRD 7.3.1).

    Plain Literal validation coerces both YAML booleans and floats to 1;
    a boolean or float scalar is the wrong input type and must fail.
    """

    @pytest.mark.parametrize(
        "value",
        ["true", "false", "1.0", "0.9", '"1"', "[1]"],
    )
    def test_wrong_type_schema_version_fails(self, value: str) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text(schema_version=value))
        message = str(excinfo.value)
        assert "config.schema_version:" in message
        # The failure is a wrong input type, not the value mismatch.
        assert "must be 1" not in message

    def test_integer_one_accepted(self) -> None:
        config = load_config_text(config_text(schema_version="1"))
        assert config.schema_version == 1


class TestUnknownFields:
    def test_root_unknown_field(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text() + "bogus: 1\n")
        assert "config.bogus:" in str(excinfo.value)

    def test_nested_unknown_field(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(
                config_text(household="  adults: 2\n  children: 1\n  recipe_servings: 3\n  pets: 9")
            )
        assert "config.household.pets:" in str(excinfo.value)

    def test_appliance_unknown_field(self) -> None:
        text = config_text().replace(
            "  microwave:\n    available: true",
            "  microwave:\n    available: true\n    turbo: true",
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.appliances.microwave.turbo:" in str(excinfo.value)


class TestDuplicateKeys:
    def test_duplicate_root_key(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text() + "retailer: ab\n")
        message = str(excinfo.value)
        assert "duplicate" in message.lower()
        assert "retailer" in message

    def test_duplicate_nested_key(self) -> None:
        text = config_text().replace(
            "  children: 1",
            "  children: 1\n  children: 1",
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        message = str(excinfo.value)
        assert "duplicate" in message.lower()
        assert "household > children" in message


class TestRanges:
    def test_negative_budget_fails(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text(weekly_budget="-1"))
        assert "config.weekly_budget:" in str(excinfo.value)

    def test_fractional_budget_ok(self) -> None:
        config = load_config_text(config_text(weekly_budget="50.5"))
        assert config.weekly_budget == Decimal("50.5")

    def test_improvement_above_100_fails(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text(material_unit_price_improvement_pct="101"))
        assert "config.material_unit_price_improvement_pct:" in str(excinfo.value)

    def test_improvement_zero_ok(self) -> None:
        config = load_config_text(config_text(material_unit_price_improvement_pct="0"))
        assert config.material_unit_price_improvement_pct == Decimal("0")

    def test_zero_household_count_fails(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(
                config_text(household="  adults: 0\n  children: 1\n  recipe_servings: 3")
            )
        assert "config.household.adults:" in str(excinfo.value)

    def test_missing_required_block_fails(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(config_text(household=""))
        assert "config.household:" in str(excinfo.value)


class TestStrictScalarTypes:
    """Wrong YAML scalar types must be rejected, not coerced (PRD 7.3.5/7.3.10)."""

    def test_quoted_int_household_count_fails(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(
                config_text(household='  adults: "2"\n  children: 1\n  recipe_servings: 3')
            )
        assert "config.household.adults:" in str(excinfo.value)

    def test_boolean_household_count_fails(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(
                config_text(household="  adults: true\n  children: 1\n  recipe_servings: 3")
            )
        assert "config.household.adults:" in str(excinfo.value)

    def test_quoted_int_staple_quantity_fails(self) -> None:
        text = config_text(
            staples=staples_text('    - key: bread\n      product_id: "1"\n      quantity: "1"')
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.staples.breakfast.0.quantity:" in str(excinfo.value)

    def test_quoted_bool_appliance_available_fails(self) -> None:
        text = config_text().replace(
            "  air_fryer:\n    available: true",
            '  air_fryer:\n    available: "yes"',
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.appliances.air_fryer.available:" in str(excinfo.value)

    def test_quoted_bool_preference_fails(self) -> None:
        text = config_text(
            appliance_preferences=(
                '  prefer_air_fryer_over_pan_frying: "false"\n'
                "  prefer_thermomix_native_over_manual: true"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.appliance_preferences.prefer_air_fryer_over_pan_frying:" in str(
            excinfo.value
        )

    def test_quoted_bool_allow_substitution_fails(self) -> None:
        text = config_text(
            staples=staples_text(
                '    - key: bread\n      product_id: "1"\n      quantity: 1\n'
                '      allow_substitution: "false"'
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.staples.breakfast.0.allow_substitution:" in str(excinfo.value)

    def test_native_yaml_scalars_still_accepted(self) -> None:
        text = config_text(
            staples=staples_text(
                '    - key: bread\n      product_id: "1"\n      quantity: 1\n'
                "      allow_substitution: true"
            )
        )
        config = load_config_text(text)
        assert config.household.adults == 2
        assert config.staples.breakfast[0].quantity == 1
        assert config.staples.breakfast[0].allow_substitution is True

    def test_quoted_decimal_budget_still_accepted(self) -> None:
        # Decimal fields stay YAML-compatible: quoted decimal strings are valid.
        config = load_config_text(config_text(weekly_budget='"50.50"'))
        assert config.weekly_budget == Decimal("50.5")


class TestStaples:
    def test_valid_staple(self) -> None:
        config = load_config_text(
            config_text(
                staples=staples_text(
                    '    - key: bread\n      product_id: "1234567"\n      quantity: 1'
                )
            )
        )
        staple = config.staples.breakfast[0]
        assert staple.key == "bread"
        assert staple.product_id == "1234567"
        assert staple.quantity == 1

    def test_staple_needs_id_or_url(self) -> None:
        text = config_text(staples=staples_text("    - key: bread\n      quantity: 1"))
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        message = str(excinfo.value)
        assert "staples.breakfast.0" in message
        assert "product_id or a canonical product_url" in message

    def test_id_and_url_must_agree(self) -> None:
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                '      product_id: "1234567"\n'
                "      product_url: https://www.ab.gr/eshop/bread/white-loaf/p/7654321\n"
                "      quantity: 1"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        message = str(excinfo.value)
        assert "staples.breakfast.0" in message
        assert "does not agree" in message

    def test_id_and_url_agree(self) -> None:
        config = load_config_text(
            config_text(
                staples=staples_text(
                    "    - key: bread\n"
                    '      product_id: "1234567"\n'
                    "      product_url: https://www.ab.gr/eshop/bread/white-loaf/p/1234567\n"
                    "      quantity: 1"
                )
            )
        )
        assert (
            config.staples.breakfast[0].product_url
            == "https://www.ab.gr/eshop/bread/white-loaf/p/1234567"
        )

    def test_category_url_fails_with_field_path(self) -> None:
        # A category (or any non-product route) URL carries no product
        # identity and fails closed at the product_url field.
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                "      product_url: https://www.ab.gr/eshop/category/bread\n"
                "      quantity: 1"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        message = str(excinfo.value)
        assert "staples.breakfast.0" in message
        assert "canonical AB.gr product URL" in message

    def test_observed_product_url_accepted(self) -> None:
        # The real observed AB.gr product page URL must validate.
        config = load_config_text(
            config_text(
                staples=staples_text(
                    "    - key: yogurt\n"
                    f"      product_url: {OBSERVED_PRODUCT_URL}\n"
                    "      quantity: 1"
                )
            )
        )
        assert config.staples.breakfast[0].product_url == OBSERVED_PRODUCT_URL

    def test_numeric_category_url_rejected_at_model_level(self) -> None:
        # Coordinator probe: a numeric category segment is not a product ID.
        with pytest.raises(ValueError):
            StapleModel(key="x", quantity=1, product_url="https://www.ab.gr/eshop/category/1234567")

    def test_dot_segment_url_rejected_at_model_level(self) -> None:
        # Coordinator probe: dot segments can change the destination and are
        # rejected, so traversal cannot smuggle an identity through.
        with pytest.raises(ValueError):
            StapleModel(
                key="x",
                quantity=1,
                product_url="https://www.ab.gr/eshop/1234567/../../checkout",
            )

    def test_encoded_traversal_url_rejected_at_model_level(self) -> None:
        # Coordinator verification (attempt 03): percent-encoded dot segments
        # decode to a different destination and must fail closed.
        with pytest.raises(ValueError):
            StapleModel(
                key="x",
                quantity=1,
                product_url="https://www.ab.gr/en/eshop/%2e%2e/p/12345",
            )

    def test_greek_url_accepted_at_model_level(self) -> None:
        url = "https://www.ab.gr/el/eshop/Γαλακτοκομικά/Γιαούρτι/p/7633916"
        staple = StapleModel(key="yogurt", quantity=1, product_id="7633916", product_url=url)
        assert staple.product_url == url

    def test_encoded_greek_url_accepted_at_model_level(self) -> None:
        # UTF-8 percent-encoding of the same Greek slug is accepted and
        # yields the same identity.
        url = "https://www.ab.gr/en/eshop/Dairy/%CE%93%CE%B9%CE%B1%CF%8C%CF%85%CF%81%CF%84%CE%B9/p/7633916"
        staple = StapleModel(key="yogurt", quantity=1, product_url=url)
        assert staple.product_url == url

    def test_trailing_colon_url_rejected_at_model_level(self) -> None:
        # Reviewer finding (attempt 04): the empty explicit port delimiter
        # (``https://www.ab.gr:/...``) is not the canonical port-free
        # authority and must fail closed at the StapleModel boundary.
        with pytest.raises(ValueError):
            StapleModel(
                key="x",
                quantity=1,
                product_url="https://www.ab.gr:/eshop/bread/white-loaf/p/12345",
            )

    def test_plain_no_port_url_accepted_at_model_level(self) -> None:
        # The ordinary canonical port-free URL remains accepted at the
        # StapleModel boundary.
        url = "https://www.ab.gr/eshop/bread/white-loaf/p/1234567"
        staple = StapleModel(key="bread", quantity=1, product_id="1234567", product_url=url)
        assert staple.product_url == url

    def test_greek_url_in_yaml(self) -> None:
        url = "https://www.ab.gr/el/eshop/Γαλακτοκομικά/Γιαούρτι/p/7633916"
        staple_line = f"    - key: yogurt\n      product_url: {url}\n      quantity: 1"
        config = load_config_text(config_text(staples=staples_text(staple_line)))
        assert config.staples.breakfast[0].product_id is None
        assert config.staples.breakfast[0].product_url == url

    def test_encoded_identity_never_normalizes_into_acceptance(self) -> None:
        # An encoded digit in the identity marker is not the raw ASCII marker
        # and must not decode into an accepted product ID.
        with pytest.raises(ValueError):
            StapleModel(
                key="x",
                quantity=1,
                product_url="https://www.ab.gr/eshop/bread/white-loaf/p/%312345",
            )

    def test_nested_encoding_rejected_at_model_level(self) -> None:
        # Double encoding survives one decode as percent text and fails.
        with pytest.raises(ValueError):
            StapleModel(
                key="x",
                quantity=1,
                product_url="https://www.ab.gr/eshop/%252e%252e/white-loaf/p/12345",
            )

    def test_encoded_traversal_url_fails_with_field_path(self) -> None:
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                "      product_url: https://www.ab.gr/eshop/%2e%2e/white-loaf/p/12345\n"
                "      quantity: 1"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.staples.breakfast.0.product_url:" in str(excinfo.value)

    def test_non_canonical_url_fails_with_field_path(self) -> None:
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                "      product_url: https://example.com/1234567\n"
                "      quantity: 1"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.staples.breakfast.0.product_url:" in str(excinfo.value)

    def test_zero_quantity_fails_with_precise_path(self) -> None:
        text = config_text(
            staples=staples_text('    - key: bread\n      product_id: "1"\n      quantity: 0')
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.staples.breakfast.0.quantity:" in str(excinfo.value)

    def test_duplicate_key_across_categories_fails(self) -> None:
        text = config_text(
            staples=(
                "  breakfast:\n"
                "    - key: bread\n"
                '      product_id: "1"\n'
                "      quantity: 1\n"
                "  lunch:\n"
                "    - key: bread\n"
                '      product_id: "2"\n'
                "      quantity: 1"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        message = str(excinfo.value)
        assert "'bread'" in message
        assert "staples.breakfast" in message
        assert "staples.lunch" in message

    def test_alternative_equal_to_preferred_fails(self) -> None:
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                '      product_id: "1"\n'
                "      quantity: 1\n"
                '      approved_alternative_product_ids: ["1"]'
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "equals the preferred product ID" in str(excinfo.value)

    def test_duplicate_alternatives_fail(self) -> None:
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                '      product_id: "1"\n'
                "      quantity: 1\n"
                '      approved_alternative_product_ids: ["2", "2"]'
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "duplicated" in str(excinfo.value)

    def test_zero_price_limit_fails_with_precise_path(self) -> None:
        text = config_text(
            staples=staples_text(
                "    - key: bread\n"
                '      product_id: "1"\n'
                "      quantity: 1\n"
                "      max_line_price_eur: 0"
            )
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.staples.breakfast.0.max_line_price_eur:" in str(excinfo.value)

    def test_positive_price_limit_ok(self) -> None:
        config = load_config_text(
            config_text(
                staples=staples_text(
                    "    - key: bread\n"
                    '      product_id: "1"\n'
                    "      quantity: 1\n"
                    "      max_product_price_eur: 3.5"
                )
            )
        )
        assert config.staples.breakfast[0].max_product_price_eur == Decimal("3.5")

    def test_appliance_capacity_must_be_positive(self) -> None:
        text = config_text().replace(
            "  air_fryer:\n    available: true",
            "  air_fryer:\n    available: true\n    capacity_litres: 0",
        )
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text(text)
        assert "config.appliances.air_fryer.capacity_litres:" in str(excinfo.value)


class TestLoaderErrors:
    def test_complex_mapping_key_fails_as_config_error(self) -> None:
        # Regression: an unhashable non-string mapping key must surface as a
        # path-aware GrossyConfigError, not a raw TypeError.
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text("? [a, b]\n: value\n")
        assert "mapping key" in str(excinfo.value)

    def test_nested_complex_mapping_key_fails_as_config_error(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text("household:\n  ? [a, b]\n  : 1\n")
        message = str(excinfo.value)
        assert "mapping key" in message
        assert "household" in message

    def test_non_string_scalar_mapping_key_fails_as_config_error(self) -> None:
        with pytest.raises(GrossyConfigError):
            load_config_text("? 1\n: value\n")

    def test_invalid_yaml_syntax(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text("retailer: [ab")
        assert "invalid YAML" in str(excinfo.value)

    def test_empty_document(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_text("   \n")
        assert "empty" in str(excinfo.value)

    def test_missing_file(self) -> None:
        with pytest.raises(GrossyConfigError) as excinfo:
            load_config_file(Path("/nonexistent/grossy/config.yaml"))
        assert "cannot read configuration file" in str(excinfo.value)


class TestProductIdFromUrl:
    """Only the canonical product route carries an unambiguous product ID.

    The supported canonical route is
    ``https://www.ab.gr/[en|el/]eshop/<category-path>/<product-slug>/p/<product-id>``
    (observed in feasibility evidence). Category and slug segments may be raw
    Unicode or percent-encoded UTF-8 (for example Greek); every ambiguous,
    category, traversal, encoded-traversal, malformed-encoding, or
    non-canonical-authority URL fails closed, and encoded text never
    normalizes into an accepted route marker or identity.
    """

    GREEK_PRODUCT_URL = (
        "https://www.ab.gr/el/eshop/Γαλακτοκομικά/Γιαούρτι/Γιαουρτι-2-Κατά-1kg/p/7633916"
    )
    ENCODED_GREEK_PRODUCT_URL = "https://www.ab.gr/en/eshop/Dairy/%CE%93%CE%B9%CE%B1%CF%8C%CF%85%CF%81%CF%84%CE%B9/p/7633916"

    def test_parses_observed_locale_product_url(self) -> None:
        assert product_id_from_url(OBSERVED_PRODUCT_URL) == "7633916"

    def test_parses_unprefixed_eshop_route(self) -> None:
        assert (
            product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf/p/1234567") == "1234567"
        )

    def test_rejects_numeric_category_url(self) -> None:
        # A numeric segment in any position other than the identity marker
        # (for example a numeric category ID) is not a product ID.
        assert product_id_from_url("https://www.ab.gr/eshop/category/1234567") is None
        assert product_id_from_url("https://www.ab.gr/eshop/1234567") is None

    def test_rejects_dot_segments(self) -> None:
        assert product_id_from_url("https://www.ab.gr/eshop/1234567/../../checkout") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread/./white-loaf/p/1") is None

    def test_rejects_encoded_traversal(self) -> None:
        # Percent-encoded dot segments and slashes decode (or normalize) to a
        # different destination; every encoding must fail closed.
        assert product_id_from_url("https://www.ab.gr/en/eshop/%2e%2e/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/en/eshop/%2E%2E/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/%2e/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%2fwhite-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/white-loaf/p/12345%2e") is None
        assert product_id_from_url("https://www.ab.gr/eshop/white-loaf/p/%252e%252e12345") is None

    def test_rejects_backslash_path_segments(self) -> None:
        # Browsers normalize backslashes to slashes, changing the destination.
        assert product_id_from_url("https://www.ab.gr/eshop/bread\\white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/en/eshop/\\../p/12345") is None

    def test_rejects_control_path_characters(self) -> None:
        # Raw C0/C1 controls, DEL, and format characters fail on the raw
        # string (urlsplit strips tabs and newlines, so the parsed path would
        # hide them).
        assert product_id_from_url("https://www.ab.gr/eshop/bread\x00/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf\n/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf\tp/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread\x7f/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread\u200b/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread\u0085/white-loaf/p/12345") is None

    def test_accepts_raw_unicode_slug(self) -> None:
        # Raw Greek (and other printable Unicode) category and slug text is
        # harmless: identity comes from the host, route, and numeric ID.
        assert product_id_from_url(self.GREEK_PRODUCT_URL) == "7633916"
        assert (
            product_id_from_url("https://www.ab.gr/eshop/ｂｒｅａｄ/white-loaf/p/12345") == "12345"
        )

    def test_accepts_percent_encoded_greek(self) -> None:
        assert product_id_from_url(self.ENCODED_GREEK_PRODUCT_URL) == "7633916"

    def test_accepts_unreserved_character_encoding(self) -> None:
        # Percent-encoding of unreserved characters (%2D = -, %7E = ~) is
        # harmless and accepted.
        assert (
            product_id_from_url("https://www.ab.gr/eshop/bread/white%2Dloaf%7E2024/p/1234567")
            == "1234567"
        )

    def test_rejects_malformed_percent_escapes(self) -> None:
        assert product_id_from_url("https://www.ab.gr/eshop/bread%2g/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%zz/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%2/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%/white-loaf/p/12345") is None

    def test_rejects_invalid_utf8_escapes(self) -> None:
        # Truncated or invalid UTF-8 byte sequences fail closed.
        assert product_id_from_url("https://www.ab.gr/eshop/bread%ce/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%ff%fe/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%ce%28/p/12345") is None

    def test_rejects_mixed_dot_traversal(self) -> None:
        # Literal, encoded, and mixed dot segments all decode to ``.`` or
        # ``..`` and are rejected.
        assert product_id_from_url("https://www.ab.gr/en/eshop/%2e./p/12345") is None
        assert product_id_from_url("https://www.ab.gr/en/eshop/.%2e/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/en/eshop/%2E%2e/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/en/eshop/%2e%2e%2f/p/12345") is None

    def test_rejects_nested_encoding(self) -> None:
        # Double encoding survives one decode as percent text; a second
        # decode is never applied, so it fails.
        assert product_id_from_url("https://www.ab.gr/eshop/%252e%252e/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/%252F/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/100%25/white-loaf/p/12345") is None

    def test_rejects_encoded_separators_and_backslashes(self) -> None:
        # Encoded path separators and backslashes normalize to separators
        # after decoding and change the destination; they fail.
        assert product_id_from_url("https://www.ab.gr/eshop/bread%2Fwhite-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%5cwhite-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%5Cwhite-loaf/p/12345") is None

    def test_rejects_decoded_controls_and_space(self) -> None:
        # Decoded control characters (including C1) and spaces fail even
        # though the raw string is plain printable ASCII.
        assert product_id_from_url("https://www.ab.gr/eshop/bread%00/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%0ap/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%7f/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%c2%85/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread%20/white-loaf/p/12345") is None

    def test_rejects_normalized_route_markers(self) -> None:
        # Route markers are raw ASCII: an encoded marker, locale, or eshop
        # segment never normalizes into acceptance.
        assert product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf/%70/12345") is None
        assert product_id_from_url("https://www.ab.gr/es%68op/bread/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr/%65n/eshop/bread/white-loaf/p/12345") is None

    def test_rejects_encoded_identity_digits(self) -> None:
        assert product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf/p/12345%32") is None
        assert product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf/p/%312345") is None

    def test_rejects_encoded_authority_forms(self) -> None:
        # The host is an exact string match; an encoded authority cannot
        # match www.ab.gr.
        assert product_id_from_url("https://www.ab.gr%2e/eshop/bread/white-loaf/p/12345") is None

    def test_rejects_empty_user_info_and_zero_port(self) -> None:
        # Reviewer note (attempt 03): truthiness guards missed empty user info
        # (username is None there) and port 0; both evade the authority policy.
        path = "/eshop/bread/white-loaf/p/12345"
        assert product_id_from_url(f"https://@www.ab.gr{path}") is None
        assert product_id_from_url(f"https://www.ab.gr:0{path}") is None

    def test_rejects_empty_port_delimiter(self) -> None:
        # Reviewer finding (attempt 04): a trailing colon is an empty port
        # delimiter. The authority is then not the exact canonical port-free
        # host and must fail closed (both the bare and locale routes).
        assert product_id_from_url("https://www.ab.gr:/eshop/bread/white-loaf/p/12345") is None
        assert product_id_from_url("https://www.ab.gr:/en/eshop/bread/white-loaf/p/12345") is None

    def test_accepts_plain_no_port_url(self) -> None:
        # The ordinary canonical port-free authority remains accepted, with
        # and without a locale segment.
        assert (
            product_id_from_url("https://www.ab.gr/el/eshop/bread/white-loaf/p/1234567")
            == "1234567"
        )
        assert (
            product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf/p/1234567") == "1234567"
        )

    def test_rejects_malformed_port(self) -> None:
        # A non-numeric port used to raise an uncaught ValueError from the
        # ``.port`` property; it must fail closed instead.
        assert product_id_from_url("https://www.ab.gr:443x/eshop/bread/white-loaf/p/12345") is None

    def test_rejects_other_hosts(self) -> None:
        assert product_id_from_url("https://example.com/eshop/bread/white-loaf/p/1234567") is None

    def test_rejects_non_https(self) -> None:
        assert product_id_from_url("http://www.ab.gr/eshop/bread/white-loaf/p/1234567") is None

    def test_rejects_user_info_port_query_fragment(self) -> None:
        base = "https://www.ab.gr/eshop/bread/white-loaf/p/1234567"
        assert (
            product_id_from_url("https://user:pw@www.ab.gr/eshop/bread/white-loaf/p/1234567")
            is None
        )
        assert product_id_from_url("https://www.ab.gr:443/eshop/bread/white-loaf/p/1234567") is None
        assert product_id_from_url(base + "?utm=1") is None
        assert product_id_from_url(base + "#ref") is None

    def test_rejects_unknown_locale(self) -> None:
        assert product_id_from_url("https://www.ab.gr/fr/eshop/bread/white-loaf/p/1234567") is None

    def test_rejects_missing_slug_before_identity(self) -> None:
        assert product_id_from_url("https://www.ab.gr/eshop/p/1234567") is None
        assert product_id_from_url("https://www.ab.gr/en/eshop/p/1234567") is None

    def test_rejects_non_numeric_id(self) -> None:
        assert product_id_from_url("https://www.ab.gr/eshop/bread/white-loaf/p/12a3") is None

    def test_rejects_non_canonical_category_url(self) -> None:
        assert product_id_from_url("https://www.ab.gr/eshop/category/bread") is None
