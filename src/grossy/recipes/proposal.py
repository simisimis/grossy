"""Versioned, schema-bound recipe-import proposals (PRD 11, FR-70..FR-77).

``grossy.recipe.import.v1`` crosses the browser/agent boundary into the
deterministic CLI when a sourced recipe is imported from a URL (PRD 9.2
``recipes add <url>``). The proposal carries only schema-authorized,
normalized facts (FR-72): factual ingredient data, concise agent-written
paraphrased steps, and appliance requirements — never source prose,
executable text, cookies, headers, DOM, screenshots, or account identity.

The proposal is a closed schema (unknown fields are rejected), uses stable
string IDs and ISO 8601 instants, and binds its own content: the ``checksum``
field is the SHA-256 of the canonical serialization of every other field.
Both ``recipes import-validate`` (dry-run preview, FR-74) and
``recipes import-apply`` recompute the checksum, so a proposal can only be
applied if it is exactly the content that was previewed (PRD 10.3.6).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from grossy.security import ProhibitedContentError, scan_prohibited_content

RECIPE_IMPORT_SCHEMA_NAME = "grossy.recipe.import.v1"
RECIPE_IMPORT_SCHEMA_VERSION = 1

# Stable recipe ID: lowercase kebab, one spelling per concept.
RECIPE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"

# Protein classes match the `recipes`/`run_recipe_slots` CHECK constraints.
PROTEIN_CLASSES = ("meat", "fish", "poultry", "vegetarian")
# Source-quality tiers (PRD FR-81), one spelling per tier. Tier *policy*
# (which tier a discovery may use) belongs to task 08; this closed set only
# fixes the deterministic vocabulary stored in `source_tier`.
SOURCE_TIERS = (
    "akis_petretzikis",
    "cuisine_native",
    "approved_established",
    "user",
    "ai_generated",
)
# Configured appliances (FR-93) plus the two conventional capabilities a
# sourced recipe may require (FR-94 equivalence: steam oven satisfies
# conventional oven; induction cooktop satisfies stovetop).
APPLIANCE_TYPES = (
    "thermomix",
    "dough_kneading_machine",
    "steam_oven",
    "air_fryer",
    "induction_cooktop",
    "microwave",
    "pressure_cooker",
    "dutch_oven",
    "conventional_oven",
    "stovetop",
)
# Match the `recipe_variants.variant_type` CHECK constraint.
VARIANT_TYPES = ("air_fryer", "thermomix_native", "other")

MAX_TITLE_LENGTH = 200
MAX_SOURCE_NAME_LENGTH = 100
MAX_CUISINE_LENGTH = 50
MAX_INGREDIENT_NAME_LENGTH = 100
MAX_UNIT_LENGTH = 30
MAX_NOTES_LENGTH = 200
# Concise paraphrased step (FR-72): single line, bounded.
MAX_SOURCED_STEP_LENGTH = 400
# User-authored steps may be stored verbatim (FR-73): multi-line, bounded.
MAX_USER_STEP_LENGTH = 1000
MAX_CAPABILITY_LENGTH = 50
MAX_MODEL_CONSTRAINT_LENGTH = 50
MAX_CAPACITY_UNIT_LENGTH = 30

MAX_INGREDIENTS = 50
MAX_STEPS = 40
MAX_APPLIANCES = 10
MAX_VARIANTS = 10

_WS_RE = re.compile(r"\s+")
_HEX_CHECKSUM_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class RecipeImportError(ValueError):
    """Raised when a recipe-import proposal fails deterministic validation."""


# --- shared field gates -------------------------------------------------------


def _check_control_chars(value: str, field: str) -> None:
    """Reject Unicode control characters (category Cc) and DEL.

    Tabs, newlines, and carriage returns are excluded: the field-specific
    gates decide whether they are collapsed (sourced single line), rejected
    (single-line fields), or preserved verbatim (user steps, FR-73).
    """
    for ch in value:
        if ch in "\t\n\r":
            continue
        if ch == "\x7f" or unicodedata.category(ch) == "Cc":
            raise RecipeImportError(f"{field} contains control or non-printable characters")


def sanitize_single_line(value: str, field: str, max_length: int) -> str:
    """Fail-closed single-line text gate.

    No newlines, no control characters, internal whitespace (spaces and
    tabs) collapsed, bounded. Used for every sourced fact (FR-72): names,
    units, titles, and concise paraphrased steps.
    """
    if "\n" in value or "\r" in value:
        raise RecipeImportError(f"{field} must be a single line")
    _check_control_chars(value, field)
    text = _WS_RE.sub(" ", value).strip()
    if not text:
        raise RecipeImportError(f"{field} must not be empty")
    if len(text) > max_length:
        raise RecipeImportError(f"{field} must be at most {max_length} characters, got {len(text)}")
    return text


def check_verbatim(value: str, field: str, max_length: int) -> str:
    """Fail-closed verbatim text gate (FR-73).

    User-authored instructions are stored exactly as written: no whitespace
    rewriting (spaces, tabs, and newlines are preserved). Only other control
    characters, emptiness, and length are gated.
    """
    _check_control_chars(value, field)
    if not value.strip():
        raise RecipeImportError(f"{field} must not be empty")
    if len(value) > max_length:
        raise RecipeImportError(
            f"{field} must be at most {max_length} characters, got {len(value)}"
        )
    return value


def _check_positive_finite(value: float, field: str) -> float:
    if not math.isfinite(value):
        raise RecipeImportError(f"{field} must be a finite number, got {value!r}")
    if value <= 0:
        raise RecipeImportError(f"{field} must be positive, got {value}")
    return value


def _check_optional_positive_finite(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    return _check_positive_finite(value, field)


def validate_reference_url(url: str) -> str:
    """Validate a canonical recipe reference URL.

    https only; no user info, port, query, fragment, backslash, or unsafe
    characters. The host is deliberately not restricted: approved recipe
    sources are not the retailer (FR-81), and source-tier policy belongs to
    discovery (task 08).
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise RecipeImportError(f"reference_url {url!r} is malformed") from None
    if parsed.scheme != "https":
        raise RecipeImportError(f"reference_url must be an https URL, got {url!r}")
    if not parsed.netloc or "@" in parsed.netloc or ":" in parsed.netloc:
        raise RecipeImportError(f"reference_url must not carry user info or a port: {url!r}")
    if parsed.query or parsed.fragment:
        raise RecipeImportError(f"reference_url must not contain a query or fragment: {url!r}")
    if "\\" in url or any(not ch.isprintable() or ch == " " for ch in url):
        raise RecipeImportError(f"reference_url {url!r} contains unsafe characters")
    if not parsed.path:
        raise RecipeImportError(f"reference_url {url!r} must carry a non-empty path")
    return url


def _check_generated_at(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecipeImportError(
            f"generated_at must be a valid ISO 8601 timestamp, got {value!r}"
        ) from None
    if parsed.tzinfo is None:
        raise RecipeImportError(
            f"generated_at must be a timezone-aware ISO 8601 instant, got {value!r}"
        )


def _format_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid"))
        return f"{location}: {message}" if location else message
    return str(exc)


# --- shared payload records ---------------------------------------------------


class RecipeIngredient(BaseModel):
    """One revision-bound normalized ingredient (FR-71)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    amount: float = Field(gt=0)
    unit: str = Field(min_length=1)
    pantry_eligible: StrictBool = False
    indivisible: StrictBool = False
    package_unit: str | None = None
    package_size: float | None = Field(default=None, gt=0)
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return sanitize_single_line(value, "ingredient name", MAX_INGREDIENT_NAME_LENGTH)

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, value: str) -> str:
        return sanitize_single_line(value, "ingredient unit", MAX_UNIT_LENGTH)

    @field_validator("package_unit")
    @classmethod
    def _check_package_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_single_line(value, "package_unit", MAX_UNIT_LENGTH)

    @field_validator("notes")
    @classmethod
    def _check_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_single_line(value, "ingredient notes", MAX_NOTES_LENGTH)

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, value: float) -> float:
        return _check_positive_finite(value, "ingredient amount")

    @field_validator("package_size")
    @classmethod
    def _check_package_size(cls, value: float | None) -> float | None:
        return _check_optional_positive_finite(value, "package_size")

    @model_validator(mode="after")
    def _check_package_pair(self) -> RecipeIngredient:
        if (self.package_size is None) != (self.package_unit is None):
            raise RecipeImportError("package_size and package_unit must be provided together")
        return self


class ApplianceRequirement(BaseModel):
    """One revision-bound appliance requirement (FR-71, FR-93, FR-96)."""

    model_config = ConfigDict(extra="forbid")

    type: str
    capability: str
    model_constraint: str | None = None
    capacity_constraint: float | None = Field(default=None, gt=0)
    capacity_unit: str | None = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if value not in APPLIANCE_TYPES:
            raise RecipeImportError(
                f"appliance type must be one of {', '.join(APPLIANCE_TYPES)}, got {value!r}"
            )
        return value

    @field_validator("capability")
    @classmethod
    def _check_capability(cls, value: str) -> str:
        return sanitize_single_line(value, "appliance capability", MAX_CAPABILITY_LENGTH)

    @field_validator("model_constraint")
    @classmethod
    def _check_model_constraint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_single_line(
            value, "appliance model_constraint", MAX_MODEL_CONSTRAINT_LENGTH
        )

    @field_validator("capacity_unit")
    @classmethod
    def _check_capacity_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_single_line(value, "capacity_unit", MAX_CAPACITY_UNIT_LENGTH)

    @field_validator("capacity_constraint")
    @classmethod
    def _check_capacity_constraint(cls, value: float | None) -> float | None:
        return _check_optional_positive_finite(value, "capacity_constraint")

    @model_validator(mode="after")
    def _check_capacity_pair(self) -> ApplianceRequirement:
        if (self.capacity_constraint is None) != (self.capacity_unit is None):
            raise RecipeImportError(
                "capacity_constraint and capacity_unit must be provided together"
            )
        return self


class RecipeVariantLink(BaseModel):
    """One linked-dish/variant relationship (FR-71, FR-99, FR-100)."""

    model_config = ConfigDict(extra="forbid")

    linked_recipe_id: str = Field(pattern=RECIPE_ID_PATTERN)
    variant_type: str

    @field_validator("variant_type")
    @classmethod
    def _check_variant_type(cls, value: str) -> str:
        if value not in VARIANT_TYPES:
            raise RecipeImportError(
                f"variant_type must be one of {', '.join(VARIANT_TYPES)}, got {value!r}"
            )
        return value


# --- sourced (URL-import) recipe payload ---------------------------------------


class ImportedRecipe(BaseModel):
    """The sourced-recipe payload of ``grossy.recipe.import.v1`` (FR-71).

    Steps are concise agent-written paraphrases (FR-72): single-line and
    bounded. A reference URL is required: user-authored content without a
    URL arrives through the YAML import instead (FR-75).
    """

    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(pattern=RECIPE_ID_PATTERN)
    title: str = Field(min_length=1)
    reference_url: str
    source_name: str = Field(min_length=1)
    source_tier: str
    cuisine: str | None = None
    protein_class: str
    total_time_minutes: StrictInt | None = Field(default=None, gt=0)
    servings: float = Field(gt=0)
    ingredients: list[RecipeIngredient]
    steps: list[str]
    appliances: list[ApplianceRequirement] = []
    variants: list[RecipeVariantLink] = []
    base_weight: float = Field(default=1.0, gt=0)
    is_favorite: StrictBool = False
    is_regular: StrictBool = True
    enabled: StrictBool = True

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        return sanitize_single_line(value, "title", MAX_TITLE_LENGTH)

    @field_validator("reference_url")
    @classmethod
    def _check_reference_url(cls, value: str) -> str:
        return validate_reference_url(value)

    @field_validator("source_name")
    @classmethod
    def _check_source_name(cls, value: str) -> str:
        return sanitize_single_line(value, "source_name", MAX_SOURCE_NAME_LENGTH)

    @field_validator("source_tier")
    @classmethod
    def _check_source_tier(cls, value: str) -> str:
        if value not in SOURCE_TIERS:
            raise RecipeImportError(
                f"source_tier must be one of {', '.join(SOURCE_TIERS)}, got {value!r}"
            )
        return value

    @field_validator("cuisine")
    @classmethod
    def _check_cuisine(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_single_line(value, "cuisine", MAX_CUISINE_LENGTH)

    @field_validator("protein_class")
    @classmethod
    def _check_protein_class(cls, value: str) -> str:
        if value not in PROTEIN_CLASSES:
            raise RecipeImportError(
                f"protein_class must be one of {', '.join(PROTEIN_CLASSES)}, got {value!r}"
            )
        return value

    @field_validator("servings")
    @classmethod
    def _check_servings(cls, value: float) -> float:
        return _check_positive_finite(value, "servings")

    @field_validator("base_weight")
    @classmethod
    def _check_base_weight(cls, value: float) -> float:
        return _check_positive_finite(value, "base_weight")

    @field_validator("steps")
    @classmethod
    def _check_steps(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= MAX_STEPS:
            raise RecipeImportError(
                f"a recipe must have between 1 and {MAX_STEPS} steps, got {len(value)}"
            )
        return [
            sanitize_single_line(step, f"steps[{i}]", MAX_SOURCED_STEP_LENGTH)
            for i, step in enumerate(value)
        ]

    @field_validator("ingredients")
    @classmethod
    def _check_ingredients(cls, value: list[RecipeIngredient]) -> list[RecipeIngredient]:
        if not 1 <= len(value) <= MAX_INGREDIENTS:
            raise RecipeImportError(
                f"a recipe must have between 1 and {MAX_INGREDIENTS} ingredients, got {len(value)}"
            )
        return value

    @field_validator("appliances")
    @classmethod
    def _check_appliances(cls, value: list[ApplianceRequirement]) -> list[ApplianceRequirement]:
        if len(value) > MAX_APPLIANCES:
            raise RecipeImportError(
                f"a recipe may have at most {MAX_APPLIANCES} appliance requirements, "
                f"got {len(value)}"
            )
        return value

    @field_validator("variants")
    @classmethod
    def _check_variants(cls, value: list[RecipeVariantLink]) -> list[RecipeVariantLink]:
        if len(value) > MAX_VARIANTS:
            raise RecipeImportError(
                f"a recipe may have at most {MAX_VARIANTS} variant links, got {len(value)}"
            )
        seen: set[tuple[str, str]] = set()
        for variant in value:
            key = (variant.linked_recipe_id, variant.variant_type)
            if key in seen:
                raise RecipeImportError(
                    f"duplicate variant link {variant.linked_recipe_id!r}/{variant.variant_type!r}"
                )
            seen.add(key)
        return value

    @model_validator(mode="after")
    def _check_state(self) -> ImportedRecipe:
        if self.is_favorite and self.is_regular:
            raise RecipeImportError(
                "a recipe cannot be both favorite and regular; favorite and "
                "regular are mutually exclusive base-weight states (FR-104)"
            )
        return self


# --- versioned envelope (grossy.recipe.import.v1) -------------------------------


class RecipeImportProposal(BaseModel):
    """Versioned, schema-bound recipe-import proposal (PRD 11).

    The ``checksum`` field binds the proposal content: it must equal the
    SHA-256 of the canonical JSON serialization of every other field. The
    binding is enforced in :func:`validate_recipe_import` over the raw
    parsed dict (the model dump may normalize numbers, so the raw form is
    the canonical one).
    """

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    schema_version: StrictInt
    generated_at: str
    recipe: ImportedRecipe
    checksum: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str) -> str:
        if value != RECIPE_IMPORT_SCHEMA_NAME:
            raise RecipeImportError(
                f"schema_name must be {RECIPE_IMPORT_SCHEMA_NAME!r}, got {value!r}"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != RECIPE_IMPORT_SCHEMA_VERSION:
            raise RecipeImportError(
                f"schema_version must be {RECIPE_IMPORT_SCHEMA_VERSION}, got {value}"
            )
        return value

    @field_validator("generated_at")
    @classmethod
    def _check_generated_at(cls, value: str) -> str:
        _check_generated_at(value)
        return value


# --- public validation (fail closed) --------------------------------------------


def proposal_checksum(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical serialization of a checksum-less payload.

    Canonical form: sorted keys, compact separators, UTF-8 (no ASCII
    escaping). The caller must pass the raw parsed proposal minus its
    ``checksum`` field.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_recipe_import(data: dict[str, Any]) -> RecipeImportProposal:
    """Validate a raw recipe-import proposal dict; fail closed on any violation.

    Order of gates: prohibited-content scan, envelope shape, version,
    checksum binding to the previewed content, then full payload
    validation. Any failure raises :class:`RecipeImportError`.
    """
    try:
        scan_prohibited_content(data)
    except ProhibitedContentError as exc:
        raise RecipeImportError(str(exc)) from exc

    expected_keys = {"schema_name", "schema_version", "generated_at", "recipe", "checksum"}
    unknown = sorted(set(data) - expected_keys)
    if unknown:
        raise RecipeImportError(f"unknown field(s) in recipe-import proposal: {', '.join(unknown)}")
    missing = sorted(expected_keys - set(data))
    if missing:
        raise RecipeImportError(f"missing field(s) in recipe-import proposal: {', '.join(missing)}")

    schema_name = data["schema_name"]
    if schema_name != RECIPE_IMPORT_SCHEMA_NAME:
        raise RecipeImportError(
            f"schema_name must be {RECIPE_IMPORT_SCHEMA_NAME!r}, got {schema_name!r}"
        )
    schema_version = data["schema_version"]
    if schema_version != RECIPE_IMPORT_SCHEMA_VERSION:
        raise RecipeImportError(
            f"schema_version must be {RECIPE_IMPORT_SCHEMA_VERSION}, got {schema_version!r}"
        )
    generated_at = data["generated_at"]
    if not isinstance(generated_at, str):
        raise RecipeImportError(f"generated_at must be a string, got {generated_at!r}")
    _check_generated_at(generated_at)

    checksum = data["checksum"]
    if not isinstance(checksum, str) or not _HEX_CHECKSUM_RE.fullmatch(checksum):
        raise RecipeImportError(f"checksum must be a 64-character hex string, got {checksum!r}")
    payload = {key: value for key, value in data.items() if key != "checksum"}
    if proposal_checksum(payload) != checksum.lower():
        raise RecipeImportError(
            "checksum does not match the proposal content; refusing an "
            "unpreviewed or modified proposal"
        )

    try:
        return RecipeImportProposal.model_validate(data)
    except ValidationError as exc:
        raise RecipeImportError(
            f"recipe-import proposal validation failed: {_format_validation_error(exc)}"
        ) from exc


def proposal_preview(proposal: RecipeImportProposal) -> dict[str, object]:
    """Build the dry-run preview payload for an accepted proposal (FR-74).

    The preview lists exactly the facts that ``import-apply`` will persist,
    so the user can approve the persisted content before any state change.
    """
    recipe = proposal.recipe
    return {
        "schema_name": "grossy.recipe.import-validate",
        "schema_version": 1,
        "status": "preview",
        "schema": RECIPE_IMPORT_SCHEMA_NAME,
        "proposal_checksum": proposal.checksum,
        "recipe_id": recipe.recipe_id,
        "title": recipe.title,
        "reference_url": recipe.reference_url,
        "source_name": recipe.source_name,
        "source_tier": recipe.source_tier,
        "cuisine": recipe.cuisine,
        "protein_class": recipe.protein_class,
        "total_time_minutes": recipe.total_time_minutes,
        "servings": recipe.servings,
        "ingredient_count": len(recipe.ingredients),
        "step_count": len(recipe.steps),
        "appliance_count": len(recipe.appliances),
        "variant_count": len(recipe.variants),
        "base_weight": recipe.base_weight,
        "is_favorite": recipe.is_favorite,
        "is_regular": recipe.is_regular,
        "enabled": recipe.enabled,
        "ingredients": [ing.model_dump() for ing in recipe.ingredients],
        "steps": list(recipe.steps),
        "appliances": [app.model_dump() for app in recipe.appliances],
        "variants": [var.model_dump() for var in recipe.variants],
    }
