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
from grossy.store import STORE_URL_ENV, StoreConfigError

# Observed store product page URL shape (feasibility task 09 evidence), on the test host.
OBSERVED_PRODUCT_URL = (
    "https://store.example/en/eshop/Dairy-Plant-Based-Beverages-and-Chilled-Products/"
    "Yogurt/Plain-yoghurts/Yogurt-Dessert-2-Fat-1kg/p/7633916"
)
GREEK_PRODUCT_URL = (
    "https://store.example/el/eshop/Γαλακτοκομικά/Γιαούρτι/Γιαουρτι-2-Κατά-1kg/p/7633916"
)
ENCODED_GREEK_PRODUCT_URL = "https://store.example/en/eshop/Dairy/%CE%93%CE%B9%CE%B1%CF%8C%CF%85%CF%81%CF%84%CE%B9/p/7633916"

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


def bread_staple(*extra_lines: str, product_id: str = '"1"', quantity: str = "1") -> str:
    """One breakfast staple block with optional extra indented lines."""
    lines = ["    - key: bread", f"      product_id: {product_id}", f"      quantity: {quantity}"]
    lines.extend(f"      {line}" for line in extra_lines)
    return staples_text("\n".join(lines))


class TestCanonicalSample:
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


@pytest.mark.parametrize(
    "text,path",
    [
        # Ranges and required blocks.
        pytest.param(
            config_text(weekly_budget="-1"), "config.weekly_budget:", id="negative-budget"
        ),
        pytest.param(
            config_text(material_unit_price_improvement_pct="101"),
            "config.material_unit_price_improvement_pct:",
            id="improvement-above-100",
        ),
        pytest.param(
            config_text(household="  adults: 0\n  children: 1\n  recipe_servings: 3"),
            "config.household.adults:",
            id="zero-household-count",
        ),
        pytest.param(config_text(household=""), "config.household:", id="missing-required-block"),
        # Wrong YAML scalar types are rejected, not coerced (PRD 7.3.5/7.3.10).
        pytest.param(
            config_text(household='  adults: "2"\n  children: 1\n  recipe_servings: 3'),
            "config.household.adults:",
            id="quoted-int-household",
        ),
        pytest.param(
            config_text(household="  adults: true\n  children: 1\n  recipe_servings: 3"),
            "config.household.adults:",
            id="boolean-household",
        ),
        pytest.param(
            config_text(staples=bread_staple(quantity='"1"')),
            "config.staples.breakfast.0.quantity:",
            id="quoted-int-staple-quantity",
        ),
        pytest.param(
            config_text().replace(
                "  air_fryer:\n    available: true", '  air_fryer:\n    available: "yes"'
            ),
            "config.appliances.air_fryer.available:",
            id="quoted-bool-appliance",
        ),
        pytest.param(
            config_text(
                appliance_preferences=(
                    '  prefer_air_fryer_over_pan_frying: "false"\n'
                    "  prefer_thermomix_native_over_manual: true"
                )
            ),
            "config.appliance_preferences.prefer_air_fryer_over_pan_frying:",
            id="quoted-bool-preference",
        ),
        pytest.param(
            config_text(staples=bread_staple('allow_substitution: "false"')),
            "config.staples.breakfast.0.allow_substitution:",
            id="quoted-bool-allow-substitution",
        ),
        # Staple field validation.
        pytest.param(
            config_text(staples=bread_staple(quantity="0")),
            "config.staples.breakfast.0.quantity:",
            id="zero-staple-quantity",
        ),
        pytest.param(
            config_text(staples=bread_staple("max_line_price_eur: 0")),
            "config.staples.breakfast.0.max_line_price_eur:",
            id="zero-price-limit",
        ),
        pytest.param(
            config_text(
                staples=staples_text(
                    "    - key: bread\n"
                    "      product_url: https://store.example/eshop/%2e%2e/white-loaf/p/12345\n"
                    "      quantity: 1"
                )
            ),
            "config.staples.breakfast.0.product_url:",
            id="encoded-traversal-url",
        ),
        pytest.param(
            config_text(
                staples=staples_text(
                    "    - key: bread\n"
                    "      product_url: https://example.com/1234567\n"
                    "      quantity: 1"
                )
            ),
            "config.staples.breakfast.0.product_url:",
            id="non-canonical-url",
        ),
        pytest.param(
            config_text().replace(
                "  air_fryer:\n    available: true",
                "  air_fryer:\n    available: true\n    capacity_litres: 0",
            ),
            "config.appliances.air_fryer.capacity_litres:",
            id="zero-appliance-capacity",
        ),
    ],
)
def test_invalid_value_fails_with_field_path(text: str, path: str) -> None:
    with pytest.raises(GrossyConfigError) as excinfo:
        load_config_text(text)
    assert path in str(excinfo.value)


@pytest.mark.parametrize(
    "text,messages",
    [
        pytest.param(
            config_text(staples=staples_text("    - key: bread\n      quantity: 1")),
            ["staples.breakfast.0", "product_id or a canonical product_url"],
            id="needs-id-or-url",
        ),
        pytest.param(
            config_text(
                staples=bread_staple(
                    "product_url: https://store.example/eshop/bread/white-loaf/p/7654321",
                    product_id='"1234567"',
                )
            ),
            ["staples.breakfast.0", "does not agree"],
            id="id-and-url-disagree",
        ),
        pytest.param(
            config_text(
                staples=staples_text(
                    "    - key: bread\n"
                    "      product_url: https://store.example/eshop/category/bread\n"
                    "      quantity: 1"
                )
            ),
            ["staples.breakfast.0", "canonical product URL of the store"],
            id="category-url",
        ),
        pytest.param(
            config_text(
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
            ),
            ["'bread'", "staples.breakfast", "staples.lunch"],
            id="duplicate-key-across-categories",
        ),
        pytest.param(
            config_text(staples=bread_staple('approved_alternative_product_ids: ["1"]')),
            ["equals the preferred product ID"],
            id="alternative-equals-preferred",
        ),
        pytest.param(
            config_text(staples=bread_staple('approved_alternative_product_ids: ["2", "2"]')),
            ["duplicated"],
            id="duplicate-alternatives",
        ),
    ],
)
def test_invalid_staple_relations_fail_with_diagnostic(text: str, messages: list[str]) -> None:
    with pytest.raises(GrossyConfigError) as excinfo:
        load_config_text(text)
    for message in messages:
        assert message in str(excinfo.value)


class TestAcceptedValues:
    def test_fractional_and_quoted_decimal_budget_ok(self) -> None:
        assert load_config_text(config_text(weekly_budget="50.5")).weekly_budget == Decimal("50.5")
        # Decimal fields stay YAML-compatible: quoted decimal strings are valid.
        assert load_config_text(config_text(weekly_budget='"50.50"')).weekly_budget == Decimal(
            "50.5"
        )

    def test_improvement_zero_ok(self) -> None:
        config = load_config_text(config_text(material_unit_price_improvement_pct="0"))
        assert config.material_unit_price_improvement_pct == Decimal("0")

    def test_native_yaml_scalars_still_accepted(self) -> None:
        config = load_config_text(
            config_text(
                staples=bread_staple(
                    "allow_substitution: true", "max_product_price_eur: 3.5", product_id='"1234567"'
                )
            )
        )
        assert config.household.adults == 2
        staple = config.staples.breakfast[0]
        assert staple.key == "bread"
        assert staple.product_id == "1234567"
        assert staple.quantity == 1
        assert staple.allow_substitution is True
        assert staple.max_product_price_eur == Decimal("3.5")

    def test_id_and_url_agree(self) -> None:
        config = load_config_text(
            config_text(
                staples=bread_staple(
                    "product_url: https://store.example/eshop/bread/white-loaf/p/1234567",
                    product_id='"1234567"',
                )
            )
        )
        assert (
            config.staples.breakfast[0].product_url
            == "https://store.example/eshop/bread/white-loaf/p/1234567"
        )

    @pytest.mark.parametrize(
        "url", [OBSERVED_PRODUCT_URL, GREEK_PRODUCT_URL], ids=["observed", "greek"]
    )
    def test_url_only_staple_in_yaml(self, url: str) -> None:
        staple_line = f"    - key: yogurt\n      product_url: {url}\n      quantity: 1"
        config = load_config_text(config_text(staples=staples_text(staple_line)))
        assert config.staples.breakfast[0].product_id is None
        assert config.staples.breakfast[0].product_url == url


class TestStapleModelUrlBoundary:
    @pytest.mark.parametrize(
        "url",
        [
            pytest.param(
                "https://store.example/eshop/category/1234567", id="numeric-category-not-product"
            ),
            pytest.param(
                "https://store.example/eshop/1234567/../../checkout", id="literal-dot-traversal"
            ),
            pytest.param(
                "https://store.example/en/eshop/%2e%2e/p/12345", id="encoded-dot-traversal"
            ),
            pytest.param(
                "https://store.example:/eshop/bread/white-loaf/p/12345", id="empty-port-delimiter"
            ),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/%312345",
                id="encoded-identity-digit",
            ),
            pytest.param(
                "https://store.example/eshop/%252e%252e/white-loaf/p/12345",
                id="nested-encoded-traversal",
            ),
        ],
    )
    def test_model_rejects_unsafe_product_url_forms(self, url: str) -> None:
        with pytest.raises(ValueError):
            StapleModel(key="x", quantity=1, product_url=url)

    @pytest.mark.parametrize(
        "url,product_id",
        [
            pytest.param(GREEK_PRODUCT_URL, "7633916", id="greek"),
            pytest.param(ENCODED_GREEK_PRODUCT_URL, None, id="percent-encoded-greek"),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/1234567", "1234567", id="plain"
            ),
        ],
    )
    def test_model_accepts_canonical_forms(self, url: str, product_id: str | None) -> None:
        staple = StapleModel(key="yogurt", quantity=1, product_id=product_id, product_url=url)
        assert staple.product_url == url


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
    ``https://store.example/[en|el/]eshop/<category-path>/<product-slug>/p/<product-id>``
    (observed in feasibility evidence). Category and slug segments may be raw
    Unicode or percent-encoded UTF-8 (for example Greek); every ambiguous,
    category, traversal, encoded-traversal, malformed-encoding, or
    non-canonical-authority URL fails closed, and encoded text never
    normalizes into an accepted route marker or identity.
    """

    @pytest.mark.parametrize(
        "url,product_id",
        [
            pytest.param(OBSERVED_PRODUCT_URL, "7633916", id="observed-locale-route"),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/1234567", "1234567", id="unprefixed"
            ),
            pytest.param(
                "https://store.example/el/eshop/bread/white-loaf/p/1234567",
                "1234567",
                id="el-locale",
            ),
            # Raw Greek (and other printable Unicode) category and slug text is
            # harmless: identity comes from the host, route, and numeric ID.
            pytest.param(GREEK_PRODUCT_URL, "7633916", id="raw-greek-slug"),
            pytest.param(
                "https://store.example/eshop/ｂｒｅａｄ/white-loaf/p/12345",
                "12345",
                id="fullwidth-slug",
            ),
            pytest.param(ENCODED_GREEK_PRODUCT_URL, "7633916", id="percent-encoded-greek"),
            # Percent-encoding of unreserved characters (%2D = -, %7E = ~) is harmless.
            pytest.param(
                "https://store.example/eshop/bread/white%2Dloaf%7E2024/p/1234567",
                "1234567",
                id="encoded-unreserved",
            ),
        ],
    )
    def test_accepts_canonical_product_route(self, url: str, product_id: str) -> None:
        assert product_id_from_url(url) == product_id

    @pytest.mark.parametrize(
        "url",
        [
            # A numeric segment anywhere but the identity marker is not a product ID.
            pytest.param("https://store.example/eshop/category/1234567", id="numeric-category"),
            pytest.param("https://store.example/eshop/1234567", id="bare-numeric-segment"),
            pytest.param("https://store.example/eshop/category/bread", id="category-route"),
            pytest.param("https://store.example/eshop/p/1234567", id="missing-slug"),
            pytest.param("https://store.example/en/eshop/p/1234567", id="missing-slug-locale"),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/12a3", id="non-numeric-id"
            ),
            # Dot segments, literal or encoded in any mix, decode to . or .. and fail.
            pytest.param("https://store.example/eshop/1234567/../../checkout", id="literal-dotdot"),
            pytest.param("https://store.example/eshop/bread/./white-loaf/p/1", id="literal-dot"),
            pytest.param("https://store.example/en/eshop/%2e%2e/p/12345", id="encoded-dotdot"),
            pytest.param(
                "https://store.example/en/eshop/%2E%2E/p/12345", id="encoded-dotdot-upper"
            ),
            pytest.param("https://store.example/eshop/%2e/white-loaf/p/12345", id="encoded-dot"),
            pytest.param("https://store.example/en/eshop/%2e./p/12345", id="mixed-dot-1"),
            pytest.param("https://store.example/en/eshop/.%2e/p/12345", id="mixed-dot-2"),
            pytest.param("https://store.example/en/eshop/%2E%2e/p/12345", id="mixed-dot-case"),
            pytest.param(
                "https://store.example/en/eshop/%2e%2e%2f/p/12345", id="encoded-dotdot-slash"
            ),
            pytest.param(
                "https://store.example/eshop/white-loaf/p/12345%2e", id="trailing-encoded-dot"
            ),
            # Double encoding is never decoded twice; it fails rather than normalizing.
            pytest.param(
                "https://store.example/eshop/%252e%252e/white-loaf/p/12345",
                id="double-encoded-dotdot",
            ),
            pytest.param(
                "https://store.example/eshop/white-loaf/p/%252e%252e12345", id="double-in-id"
            ),
            pytest.param(
                "https://store.example/eshop/%252F/white-loaf/p/12345", id="double-encoded-slash"
            ),
            pytest.param(
                "https://store.example/eshop/100%25/white-loaf/p/12345", id="encoded-percent"
            ),
            # Encoded separators and backslashes normalize to separators after decoding.
            pytest.param(
                "https://store.example/eshop/bread%2fwhite-loaf/p/12345", id="encoded-slash"
            ),
            pytest.param(
                "https://store.example/eshop/bread%2Fwhite-loaf/p/12345", id="encoded-slash-upper"
            ),
            pytest.param(
                "https://store.example/eshop/bread%5cwhite-loaf/p/12345", id="encoded-backslash"
            ),
            pytest.param(
                "https://store.example/eshop/bread%5Cwhite-loaf/p/12345",
                id="encoded-backslash-upper",
            ),
            pytest.param(
                "https://store.example/eshop/bread\\white-loaf/p/12345", id="raw-backslash"
            ),
            pytest.param("https://store.example/en/eshop/\\../p/12345", id="raw-backslash-dotdot"),
            # Raw controls, DEL and format characters fail on the raw string
            # (urlsplit strips tabs/newlines, so the parsed path would hide them).
            pytest.param("https://store.example/eshop/bread\x00/white-loaf/p/12345", id="raw-nul"),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf\n/p/12345", id="raw-newline"
            ),
            pytest.param("https://store.example/eshop/bread/white-loaf\tp/12345", id="raw-tab"),
            pytest.param("https://store.example/eshop/bread\x7f/white-loaf/p/12345", id="raw-del"),
            pytest.param(
                "https://store.example/eshop/bread\u200b/white-loaf/p/12345", id="raw-zwsp"
            ),
            pytest.param(
                "https://store.example/eshop/bread\u0085/white-loaf/p/12345", id="raw-nel"
            ),
            # Decoded controls (including C1) and spaces fail even from plain ASCII input.
            pytest.param(
                "https://store.example/eshop/bread%00/white-loaf/p/12345", id="encoded-nul"
            ),
            pytest.param("https://store.example/eshop/bread%0ap/12345", id="encoded-newline"),
            pytest.param(
                "https://store.example/eshop/bread%7f/white-loaf/p/12345", id="encoded-del"
            ),
            pytest.param(
                "https://store.example/eshop/bread%c2%85/white-loaf/p/12345", id="encoded-nel"
            ),
            pytest.param(
                "https://store.example/eshop/bread%20/white-loaf/p/12345", id="encoded-space"
            ),
            # Malformed or invalid-UTF-8 percent escapes fail closed.
            pytest.param("https://store.example/eshop/bread%2g/white-loaf/p/12345", id="bad-hex-1"),
            pytest.param("https://store.example/eshop/bread%zz/white-loaf/p/12345", id="bad-hex-2"),
            pytest.param(
                "https://store.example/eshop/bread%2/white-loaf/p/12345", id="short-escape"
            ),
            pytest.param(
                "https://store.example/eshop/bread%/white-loaf/p/12345", id="bare-percent"
            ),
            pytest.param("https://store.example/eshop/bread%ce/p/12345", id="truncated-utf8"),
            pytest.param("https://store.example/eshop/bread%ff%fe/p/12345", id="invalid-utf8"),
            pytest.param(
                "https://store.example/eshop/bread%ce%28/p/12345", id="invalid-utf8-continuation"
            ),
            # Route markers are raw ASCII: encoded forms never normalize into acceptance.
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/%70/12345", id="encoded-p-marker"
            ),
            pytest.param(
                "https://store.example/es%68op/bread/white-loaf/p/12345", id="encoded-eshop"
            ),
            pytest.param(
                "https://store.example/%65n/eshop/bread/white-loaf/p/12345", id="encoded-locale"
            ),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/12345%32", id="encoded-id-digit"
            ),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/%312345", id="encoded-id-lead"
            ),
            # Authority must be exactly the canonical port-free host over https.
            pytest.param(
                "https://store.example%2e/eshop/bread/white-loaf/p/12345", id="encoded-authority"
            ),
            pytest.param(
                "https://@store.example/eshop/bread/white-loaf/p/12345", id="empty-user-info"
            ),
            pytest.param(
                "https://user:pw@store.example/eshop/bread/white-loaf/p/1234567", id="user-info"
            ),
            pytest.param("https://store.example:0/eshop/bread/white-loaf/p/12345", id="zero-port"),
            pytest.param(
                "https://store.example:443/eshop/bread/white-loaf/p/1234567", id="explicit-port"
            ),
            pytest.param(
                "https://store.example:443x/eshop/bread/white-loaf/p/12345", id="malformed-port"
            ),
            pytest.param("https://store.example:/eshop/bread/white-loaf/p/12345", id="empty-port"),
            pytest.param(
                "https://store.example:/en/eshop/bread/white-loaf/p/12345", id="empty-port-locale"
            ),
            pytest.param("https://example.com/eshop/bread/white-loaf/p/1234567", id="foreign-host"),
            pytest.param("http://store.example/eshop/bread/white-loaf/p/1234567", id="non-https"),
            pytest.param(
                "https://store.example/fr/eshop/bread/white-loaf/p/1234567", id="unknown-locale"
            ),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/1234567?utm=1", id="query"
            ),
            pytest.param(
                "https://store.example/eshop/bread/white-loaf/p/1234567#ref", id="fragment"
            ),
        ],
    )
    def test_rejects_ambiguous_or_noncanonical_url(self, url: str) -> None:
        assert product_id_from_url(url) is None

    def test_host_rule_follows_the_store_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The accepted host is whatever ``GROSSY_STORE_URL`` names, nothing else."""
        other_host_url = "https://other.example/en/eshop/bread/white-loaf/p/1234567"
        assert product_id_from_url(OBSERVED_PRODUCT_URL) == "7633916"
        assert product_id_from_url(other_host_url) is None
        monkeypatch.setenv(STORE_URL_ENV, "https://other.example")
        assert product_id_from_url(OBSERVED_PRODUCT_URL) is None
        assert product_id_from_url(other_host_url) == "1234567"

    def test_missing_store_setting_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(STORE_URL_ENV)
        with pytest.raises(StoreConfigError, match=STORE_URL_ENV):
            product_id_from_url(OBSERVED_PRODUCT_URL)
        with pytest.raises(GrossyConfigError, match=STORE_URL_ENV):
            load_config_text(
                config_text(
                    staples=staples_text(
                        f"    - key: yogurt\n      product_url: {OBSERVED_PRODUCT_URL}\n"
                        "      quantity: 1"
                    )
                )
            )
