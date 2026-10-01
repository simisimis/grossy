"""User-authored recipe YAML import (PRD FR-73, FR-75; task 07).

A ``grossy.recipe.yaml.v1`` file imports a recipe with no public URL
(FR-75): ``reference_url``, ``source_name``, and ``source_tier`` are all
optional and default to the user tier. User-authored instructions may be
stored verbatim (FR-73): steps are gated for control characters, emptiness,
and length, but are never whitespace-rewritten.

The file validates the same deterministic invariants as the URL-import
proposal: closed schema (unknown fields rejected), stable recipe ID, enum
values, positive finite quantities, pantry/indivisible flags, appliance
requirements, variant links, and favorite/regular mutual exclusion. The
strict loader also rejects duplicate YAML keys.
"""

from __future__ import annotations

import yaml
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

from grossy.config.loader import (
    GrossyConfigError,
    _StrictYamlLoader,
)
from grossy.recipes.proposal import (
    MAX_APPLIANCES,
    MAX_CUISINE_LENGTH,
    MAX_INGREDIENTS,
    MAX_SOURCE_NAME_LENGTH,
    MAX_STEPS,
    MAX_TITLE_LENGTH,
    MAX_USER_STEP_LENGTH,
    MAX_VARIANTS,
    PROTEIN_CLASSES,
    RECIPE_ID_PATTERN,
    SOURCE_TIERS,
    ApplianceRequirement,
    RecipeIngredient,
    RecipeVariantLink,
    _check_positive_finite,
    check_verbatim,
    sanitize_single_line,
    validate_reference_url,
)

RECIPE_YAML_SCHEMA_NAME = "grossy.recipe.yaml.v1"
RECIPE_YAML_SCHEMA_VERSION = 1

# Provenance values recorded on revisions (one spelling per concept).
PROVENANCE_URL_IMPORT = "url-import"
PROVENANCE_USER_FILE = "user-file"


class RecipeYamlError(ValueError):
    """Raised when a user-authored recipe YAML file fails validation."""


class UserRecipe(BaseModel):
    """The recipe payload of a ``grossy.recipe.yaml.v1`` file.

    Differs from the sourced :class:`ImportedRecipe` only where the PRD
    differs: no URL is required (FR-75) and steps are verbatim (FR-73).
    """

    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(pattern=RECIPE_ID_PATTERN)
    title: str = Field(min_length=1)
    reference_url: str | None = None
    source_name: str | None = None
    source_tier: str | None = None
    cuisine: str | None = None
    protein_class: str
    total_time_minutes: StrictInt | None = Field(default=None, gt=0)
    # Optional: when absent, the import applies the household target
    # (config `household.recipe_servings`) as the source servings.
    servings: float | None = Field(default=None, gt=0)
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
    def _check_reference_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_reference_url(value)

    @field_validator("source_name")
    @classmethod
    def _check_source_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_single_line(value, "source_name", MAX_SOURCE_NAME_LENGTH)

    @field_validator("source_tier")
    @classmethod
    def _check_source_tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in SOURCE_TIERS:
            raise RecipeYamlError(
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
            raise RecipeYamlError(
                f"protein_class must be one of {', '.join(PROTEIN_CLASSES)}, got {value!r}"
            )
        return value

    @field_validator("servings")
    @classmethod
    def _check_servings(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _check_positive_finite(value, "servings")

    @field_validator("base_weight")
    @classmethod
    def _check_base_weight(cls, value: float) -> float:
        return _check_positive_finite(value, "base_weight")

    @field_validator("steps")
    @classmethod
    def _check_steps(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= MAX_STEPS:
            raise RecipeYamlError(
                f"a recipe must have between 1 and {MAX_STEPS} steps, got {len(value)}"
            )
        # Verbatim (FR-73): stored exactly as written, only gated.
        return [
            check_verbatim(step, f"steps[{i}]", MAX_USER_STEP_LENGTH)
            for i, step in enumerate(value)
        ]

    @field_validator("ingredients")
    @classmethod
    def _check_ingredients(cls, value: list[RecipeIngredient]) -> list[RecipeIngredient]:
        if not 1 <= len(value) <= MAX_INGREDIENTS:
            raise RecipeYamlError(
                f"a recipe must have between 1 and {MAX_INGREDIENTS} ingredients, got {len(value)}"
            )
        return value

    @field_validator("appliances")
    @classmethod
    def _check_appliances(cls, value: list[ApplianceRequirement]) -> list[ApplianceRequirement]:
        if len(value) > MAX_APPLIANCES:
            raise RecipeYamlError(
                f"a recipe may have at most {MAX_APPLIANCES} appliance requirements, "
                f"got {len(value)}"
            )
        return value

    @field_validator("variants")
    @classmethod
    def _check_variants(cls, value: list[RecipeVariantLink]) -> list[RecipeVariantLink]:
        if len(value) > MAX_VARIANTS:
            raise RecipeYamlError(
                f"a recipe may have at most {MAX_VARIANTS} variant links, got {len(value)}"
            )
        seen: set[tuple[str, str]] = set()
        for variant in value:
            key = (variant.linked_recipe_id, variant.variant_type)
            if key in seen:
                raise RecipeYamlError(
                    f"duplicate variant link {variant.linked_recipe_id!r}/{variant.variant_type!r}"
                )
            seen.add(key)
        return value

    @model_validator(mode="after")
    def _check_state(self) -> UserRecipe:
        if self.is_favorite and self.is_regular:
            raise RecipeYamlError(
                "a recipe cannot be both favorite and regular; favorite and "
                "regular are mutually exclusive base-weight states (FR-104)"
            )
        return self


class RecipeYamlFile(BaseModel):
    """Versioned, schema-bound user-authored recipe file (PRD 11)."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    schema_version: StrictInt
    recipe: UserRecipe

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str) -> str:
        if value != RECIPE_YAML_SCHEMA_NAME:
            raise RecipeYamlError(f"schema_name must be {RECIPE_YAML_SCHEMA_NAME!r}, got {value!r}")
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != RECIPE_YAML_SCHEMA_VERSION:
            raise RecipeYamlError(
                f"schema_version must be {RECIPE_YAML_SCHEMA_VERSION}, got {value}"
            )
        return value


def _format_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid"))
        return f"{location}: {message}" if location else message
    return str(exc)


def load_recipe_yaml(text: str) -> UserRecipe:
    """Parse and strictly validate a user-authored recipe YAML document.

    Fails closed with :class:`RecipeYamlError` on malformed YAML, duplicate
    keys, unknown or missing fields, wrong schema version, or any payload
    invariant violation.
    """
    try:
        data = yaml.load(text, Loader=_StrictYamlLoader)
    except GrossyConfigError as exc:
        # Duplicate keys and non-string mapping keys surface as config
        # loader errors; re-report them as recipe-file errors.
        raise RecipeYamlError(str(exc)) from exc
    except yaml.YAMLError as exc:
        reason = str(exc).replace("\n", " ")
        raise RecipeYamlError(f"invalid YAML: {reason}") from exc
    if data is None:
        raise RecipeYamlError("recipe file is empty")
    if not isinstance(data, dict):
        raise RecipeYamlError("recipe file must be a YAML mapping")

    expected_keys = {"schema_name", "schema_version", "recipe"}
    unknown = sorted(set(data) - expected_keys)
    if unknown:
        raise RecipeYamlError(f"unknown field(s) in recipe file: {', '.join(unknown)}")
    missing = sorted(expected_keys - set(data))
    if missing:
        raise RecipeYamlError(f"missing field(s) in recipe file: {', '.join(missing)}")
    schema_name = data["schema_name"]
    if schema_name != RECIPE_YAML_SCHEMA_NAME:
        raise RecipeYamlError(
            f"schema_name must be {RECIPE_YAML_SCHEMA_NAME!r}, got {schema_name!r}"
        )
    schema_version = data["schema_version"]
    if schema_version != RECIPE_YAML_SCHEMA_VERSION:
        raise RecipeYamlError(
            f"schema_version must be {RECIPE_YAML_SCHEMA_VERSION}, got {schema_version!r}"
        )

    try:
        parsed = RecipeYamlFile.model_validate(data)
    except ValidationError as exc:
        raise RecipeYamlError(
            f"recipe file validation failed: {_format_validation_error(exc)}"
        ) from exc
    return parsed.recipe


def apply_yaml_defaults(recipe: UserRecipe, default_servings: int) -> UserRecipe:
    """Fill the user-tier defaults for a validated user recipe.

    - ``servings`` defaults to the household target (config
      ``household.recipe_servings``): user recipes are authored for the
      household, so an omitted servings value means "already at target".
    - ``source_tier`` defaults to ``user`` and ``source_name`` to ``user``.

    Returns a new model; the input is not mutated.
    """
    update: dict[str, object] = {}
    if recipe.servings is None:
        update["servings"] = float(default_servings)
    if recipe.source_tier is None:
        update["source_tier"] = "user"
    if recipe.source_name is None:
        update["source_name"] = "user"
    if not update:
        return recipe
    return recipe.model_copy(update=update)
