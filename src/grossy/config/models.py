"""Pydantic models for the strict Grossy YAML policy configuration.

Implements the configuration contract of PRD section 7.2: exact literals,
positive ranges, staple identity/uniqueness rules, and rejection of unknown
fields at every level.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import unquote_to_bytes, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

NUMERIC_ID_PATTERN = r"^[0-9]+$"

PRODUCT_URL_HOST = "www.ab.gr"
# AB.gr renders product pages under /eshop/ with an optional locale segment;
# only the two locales observed in the sanctioned interface are accepted.
_PRODUCT_URL_LOCALES = ("en", "el")

_ID_SEGMENT_RE = re.compile(r"^[0-9]+$")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class _StrictModel(BaseModel):
    """Base model: unknown fields are validation errors (PRD 7.3.14).

    Integer and boolean fields are declared with ``StrictInt``/``StrictBool``
    so wrong YAML scalar types (for example ``adults: "2"`` or
    ``available: "yes"``) are rejected instead of coerced. Decimal fields stay
    lax so YAML numerals and quoted decimal strings both remain valid.
    """

    model_config = ConfigDict(extra="forbid")


PositiveDecimal = Annotated[Decimal, Field(gt=0)]
PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]


class HouseholdModel(_StrictModel):
    adults: PositiveStrictInt
    children: PositiveStrictInt
    recipe_servings: PositiveStrictInt


class ThermomixModel(_StrictModel):
    available: StrictBool
    model: str | None = None


class AirFryerModel(_StrictModel):
    available: StrictBool
    capacity_litres: PositiveDecimal | None = None


class MicrowaveModel(_StrictModel):
    available: StrictBool
    wattage: PositiveDecimal | None = None


class PressureCookerModel(_StrictModel):
    available: StrictBool
    kind: str | None = None
    capacity_litres: PositiveDecimal | None = None


class DutchOvenModel(_StrictModel):
    available: StrictBool
    capacity_litres: PositiveDecimal | None = None


class SimpleApplianceModel(_StrictModel):
    available: StrictBool


class AppliancesModel(_StrictModel):
    thermomix: ThermomixModel
    dough_kneading_machine: SimpleApplianceModel
    steam_oven: SimpleApplianceModel
    air_fryer: AirFryerModel
    induction_cooktop: SimpleApplianceModel
    microwave: MicrowaveModel
    pressure_cooker: PressureCookerModel
    dutch_oven: DutchOvenModel


class AppliancePreferencesModel(_StrictModel):
    prefer_air_fryer_over_pan_frying: StrictBool
    prefer_thermomix_native_over_manual: StrictBool


def _is_safe_url_char(char: str) -> bool:
    """Raw URL text: printable, non-space, non-angle-bracket.

    ``str.isprintable`` rejects C0/C1 controls, DEL, format characters
    (zero-width, BOM), and lone surrogates; space is rejected explicitly
    because it is not a legal URL character. Angle brackets (``<`` and
    ``>``) are rejected because no legitimate AB.gr product URL segment
    contains them, and their presence after percent-decoding would permit
    executable markup (e.g. ``%3Cscript%3E``) to bypass the raw-string
    prohibited-content scan in the observation envelope.
    """
    return char.isprintable() and char not in (" ", "<", ">")


def _decode_path_segment(segment: str) -> str | None:
    """Percent-decode one raw path segment once; ``None`` if unsafe.

    Decoding is applied to path segments only (never to the authority or the
    whole URL). The decoded text must stay in the narrow accepted language:

    - built only from well-formed ``%XX`` escapes (malformed escapes fail);
    - valid UTF-8 bytes (truncated or invalid escape sequences fail);
    - printable, non-space text (decoded controls such as ``%00``, ``%0a``,
      ``%7f``, or ``%c2%85`` fail);
    - no path separator (``%2f`` fails) and no backslash (``%5c`` fails);
    - no ``.`` or ``..`` segment (literal, encoded, or mixed dot traversal
      such as ``%2e%2e`` or ``%2e.`` fails);
    - no remaining percent sign (nested/double encoding such as
      ``%252e%252e`` would need a second decode and fails).
    """
    i = segment.find("%")
    while i != -1:
        if (
            i + 2 >= len(segment)
            or segment[i + 1] not in _HEX_DIGITS
            or segment[i + 2] not in _HEX_DIGITS
        ):
            return None
        i = segment.find("%", i + 3)
    try:
        decoded = unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if (
        decoded in (".", "..")
        or "/" in decoded
        or "\\" in decoded
        or "%" in decoded
        or any(not _is_safe_url_char(char) for char in decoded)
    ):
        return None
    return decoded


def product_id_from_url(product_url: str) -> str | None:
    """Deterministically parse the stable AB.gr product ID from a canonical URL.

    The only supported canonical product route is the rendered AB.gr product
    page:

    ``https://www.ab.gr/[<locale>/]eshop/<category-path>/<product-slug>/p/<product-id>``

    for example
    ``https://www.ab.gr/en/eshop/Dairy-.../Yogurt-Dessert-2-Fat-1kg/p/7633916``.
    Everything else fails closed. Narrow accepted language:

    - scheme ``https`` with the exact canonical port-free authority
      ``www.ab.gr``: no user info (including an empty ``https://@`` form), no
      port (including ``:0`` and the empty ``:`` port delimiter), no query, no
      fragment, and no malformed, encoded, or differently-cased host
      (product identity must not depend on any of them; percent-encoded
      authority forms cannot match the exact host);
    - the raw URL contains no backslashes and only printable, non-space
      characters: no C0/C1 controls, no DEL, no format characters, no lone
      surrogates (``urlsplit`` strips tabs and newlines, so the raw string is
      checked, not the parsed path);
    - the path has no empty, ``.`` or ``..`` segments;
    - every path segment is percent-decoded exactly once, and the decoded
      segment must stay in the narrow accepted language (see
      ``_decode_path_segment``); malformed escapes, invalid UTF-8, encoded or
      raw path separators, encoded or raw backslashes, literal/encoded/mixed
      dot traversal, nested encoding, and decoded controls all fail;
    - category and slug segments may be raw Unicode or percent-encoded UTF-8
      (for example Greek); the decoded text is safety-checked only and never
      used for identity;
    - route markers are raw ASCII: the path starts with ``/eshop/`` or
      ``/<locale>/eshop/`` where ``<locale>`` is ``en`` or ``el``; the final
      two path segments are exactly ``/p/<product-id>`` with an all-digit
      ``<product-id>``; at least one segment sits between ``/eshop`` and the
      identity marker. An encoded marker (for example ``%70`` for ``p``) or an
      encoded identity digit never normalizes into acceptance.

    A numeric segment in any other position (for example a numeric category
    ID) is not evidence of a product ID and is rejected. These are
    compatibility rules for user-authored configuration, not claims about
    which URL forms the live retailer exposes.
    """
    try:
        parsed = urlsplit(product_url)
    except ValueError:
        # Malformed authority (for example an invalid IPv6 literal).
        return None
    # The authority must be the exact canonical port-free host: this rejects
    # any user info (including the empty ``https://@`` form), any port
    # (including the empty ``:`` port delimiter and non-numeric ports such as
    # ``:443x``), and differently-cased or percent-encoded host forms.
    if parsed.scheme != "https" or parsed.netloc != PRODUCT_URL_HOST:
        return None
    if parsed.query or parsed.fragment:
        return None
    # Raw-string gate: backslashes (browsers normalize them to slashes) and
    # any non-printable or space character are rejected on the raw string;
    # ``urlsplit`` strips tabs and newlines, so the parsed path would hide
    # them.
    if "\\" in product_url or any(not _is_safe_url_char(char) for char in product_url):
        return None
    path = parsed.path
    segments = path.split("/")
    # segments[0] is always the empty string before the leading slash.
    if any(segment in ("", ".", "..") for segment in segments[1:]):
        return None
    for segment in segments[1:]:
        if _decode_path_segment(segment) is None:
            return None
    if len(segments) < 3 or segments[-2] != "p" or not _ID_SEGMENT_RE.fullmatch(segments[-1]):
        return None
    eshop_index = 1 if segments[1] == "eshop" else (2 if segments[2] == "eshop" else None)
    if eshop_index is None:
        return None
    if eshop_index == 2 and segments[1] not in _PRODUCT_URL_LOCALES:
        return None
    # At least one segment (the product slug) between /eshop and /p/<id>.
    if len(segments) - 2 < eshop_index + 2:
        return None
    return segments[-1]


class StapleModel(_StrictModel):
    key: Annotated[str, Field(min_length=1)]
    product_id: Annotated[str, Field(pattern=NUMERIC_ID_PATTERN)] | None = None
    product_url: str | None = None
    quantity: PositiveStrictInt
    unit_preference: str | None = None
    package_preference: str | None = None
    approved_alternative_product_ids: list[Annotated[str, Field(pattern=NUMERIC_ID_PATTERN)]] = []
    allow_substitution: StrictBool = False
    max_product_price_eur: PositiveDecimal | None = None
    max_line_price_eur: PositiveDecimal | None = None

    @field_validator("product_url")
    @classmethod
    def _check_product_url(cls, value: str | None) -> str | None:
        if value is not None and product_id_from_url(value) is None:
            raise ValueError(
                "must be a supported canonical AB.gr product URL of the form "
                "https://www.ab.gr/[en|el/]eshop/<category-path>/<product-slug>/p/<product-id>"
            )
        return value

    @model_validator(mode="after")
    def _check_identity(self) -> StapleModel:
        if self.product_id is None and self.product_url is None:
            raise ValueError(f"staple {self.key!r} requires product_id or a canonical product_url")
        if self.product_url is not None:
            url_id = product_id_from_url(self.product_url)
            if url_id is None:
                raise ValueError(
                    f"staple {self.key!r}: product_url {self.product_url!r} does not carry a "
                    "parseable AB.gr product ID"
                )
        if self.product_id is not None and self.product_url is not None:
            url_id = product_id_from_url(self.product_url)
            if url_id is None or url_id != self.product_id:
                raise ValueError(
                    f"staple {self.key!r}: product_id {self.product_id!r} does not agree with "
                    f"the product ID carried by product_url {self.product_url!r}"
                )
        preferred_id = self.product_id
        if preferred_id is None and self.product_url is not None:
            preferred_id = product_id_from_url(self.product_url)
        seen: set[str] = set()
        for alternative in self.approved_alternative_product_ids:
            if alternative in seen:
                raise ValueError(
                    f"staple {self.key!r}: alternative product ID {alternative!r} is duplicated"
                )
            seen.add(alternative)
            if alternative == preferred_id:
                raise ValueError(
                    f"staple {self.key!r}: alternative product ID {alternative!r} equals the "
                    "preferred product ID"
                )
        return self


class StaplesModel(_StrictModel):
    # The only predefined recurring categories (PRD 7.3.19).
    breakfast: list[StapleModel] = []
    lunch: list[StapleModel] = []


class GrossyConfigModel(_StrictModel):
    # StrictInt rejects wrong scalar types (``true`` and ``1.0`` coerce to 1
    # under plain Literal validation); the validator pins the exact value.
    schema_version: StrictInt
    retailer: Literal["ab"]
    timezone: Literal["Europe/Athens"]
    currency: Literal["EUR"]
    household: HouseholdModel
    weekly_budget: Annotated[Decimal, Field(gt=0)] = Decimal("100")
    material_unit_price_improvement_pct: Annotated[Decimal, Field(ge=0, le=100)] = Decimal("15")
    disliked_ingredients: list[Annotated[str, Field(min_length=1)]] = []
    pantry_assumptions: list[Annotated[str, Field(min_length=1)]] = []
    appliances: AppliancesModel
    appliance_preferences: AppliancePreferencesModel
    staples: StaplesModel

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("must be 1")
        return value

    @model_validator(mode="after")
    def _check_staple_keys_unique(self) -> GrossyConfigModel:
        seen: dict[str, str] = {}
        for category in ("breakfast", "lunch"):
            for staple in getattr(self.staples, category):
                previous = seen.get(staple.key)
                if previous is not None:
                    raise ValueError(
                        f"staple key {staple.key!r} is duplicated: present in both "
                        f"'staples.{previous}' and 'staples.{category}'"
                    )
                seen[staple.key] = category
        return self
