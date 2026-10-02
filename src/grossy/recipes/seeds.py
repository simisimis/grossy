"""Approved seed recipe definitions and controlled import (PRD FR-86..FR-88; task 08).

The four approved seed recipes are defined with their canonical source URLs,
required interpretations, and provenance. The seed import process attempts to
materialize each seed from an approved sanitized fixture file. If a fixture
is missing or the source is inaccessible, the seed is reported as "controlled
missing" — never silently substituted or AI-generated (FR-88).

Seeds are imported as enabled, regular recipes with base weight 1 (FR-86).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import (
    ImportedRecipe,
    RecipeImportError,
    validate_recipe_import,
)
from grossy.recipes.repository import (
    RecipeStorageError,
    insert_recipe_with_revision,
    recipe_exists,
)

# --- approved seed definitions (FR-86) ------------------------------------------

SEED_SPECS: list[dict[str, str]] = [
    {
        "recipe_id": "air-fryer-pork-schnitzel",
        "title": "Air-fryer pork schnitzel with fries",
        "protein_class": "meat",
        "reference_url": "https://akispetretzikis.com/en/recipe/8684/snitsel-choirino-me-patates-sto-air-fryer",
        "source_name": "Akis Petretzikis",
        "source_tier": "akis_petretzikis",
        "interpretation": "Store as an air-fryer-native recipe.",
    },
    {
        "recipe_id": "air-fryer-ginger-soy-salmon",
        "title": "Air-fryer ginger-and-soy salmon with broccoli",
        "protein_class": "fish",
        "reference_url": "https://www.foodnetwork.com/recipes/food-network-kitchen/ginger-and-soy-salmon-fillets-with-broccoli-5277868",
        "source_name": "Food Network",
        "source_tier": "approved_established",
        "interpretation": "Approved local revision uses teriyaki sauce and "
        "includes rice; preserve provenance and the approved revised facts.",
    },
    {
        "recipe_id": "chicken-alfredo",
        "title": "Chicken Alfredo",
        "protein_class": "poultry",
        "reference_url": "https://www.bbcgoodfood.com/recipes/chicken-alfredo",
        "source_name": "BBC Good Food",
        "source_tier": "approved_established",
        "interpretation": "Approved local revision uses chicken thighs; "
        "preserve provenance and the approved revised facts.",
    },
    {
        "recipe_id": "spicy-bean-burgers",
        "title": "Spicy bean burgers",
        "protein_class": "vegetarian",
        "reference_url": "https://www.bbcgoodfood.com/recipes/mexican-bean-burgers-lime-yogurt-salsa/",
        "source_name": "BBC Good Food",
        "source_tier": "approved_established",
        "interpretation": "Store as the approved vegetarian seed.",
    },
]

SEED_PROVENANCE = "seed-import"
SEED_FIXTURE_DIRNAME = "seeds"

# Approved sanitized seed fixtures shipped with the package. Fresh
# installation bootstraps these (FR-86); seeds without a bundled fixture
# are reported as controlled missing (FR-88) and imported later through the
# controlled import path.
BUNDLED_SEED_FIXTURE_DIR = Path(__file__).resolve().parent / "seed_fixtures"

# Checkable content requirements derived from each seed's approved
# interpretation (FR-86): appliance types the recipe must require and
# ingredient substrings that must appear in the fixture. A fixture that
# omits them does not match the approved seed and is rejected.
SEED_CONTENT_REQUIREMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "air-fryer-pork-schnitzel": {
        # "Store as an air-fryer-native recipe."
        "required_appliance_types": ("air_fryer",),
        "required_ingredient_substrings": (),
    },
    "air-fryer-ginger-soy-salmon": {
        # "...uses teriyaki sauce and includes rice..."
        "required_appliance_types": (),
        "required_ingredient_substrings": ("teriyaki", "rice"),
    },
    "chicken-alfredo": {
        # "...uses chicken thighs..."
        "required_appliance_types": (),
        "required_ingredient_substrings": ("chicken thigh",),
    },
    "spicy-bean-burgers": {
        # "Store as the approved vegetarian seed." (class checked on the spec)
        "required_appliance_types": (),
        "required_ingredient_substrings": (),
    },
}


class SeedImportError(Exception):
    """Raised when seed import fails."""


@dataclass(frozen=True)
class SeedStatus:
    """Status of one seed after the import attempt."""

    recipe_id: str
    title: str
    protein_class: str
    status: str  # "imported" | "already_exists" | "missing_fixture" | "fixture_invalid"
    detail: str = ""
    revision_id: int | None = None


@dataclass(frozen=True)
class SeedImportResult:
    """Result of the seed import operation."""

    seeds: list[SeedStatus] = field(default_factory=list)
    imported_count: int = 0
    existing_count: int = 0
    missing_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": "grossy.recipes.seed",
            "schema_version": 1,
            "status": "complete",
            "imported_count": self.imported_count,
            "existing_count": self.existing_count,
            "missing_count": self.missing_count,
            "seeds": [
                {
                    "recipe_id": s.recipe_id,
                    "title": s.title,
                    "protein_class": s.protein_class,
                    "status": s.status,
                    "detail": s.detail,
                    "revision_id": s.revision_id,
                }
                for s in self.seeds
            ],
            "controlled_missing_seeds": [
                s.recipe_id
                for s in self.seeds
                if s.status in ("missing_fixture", "fixture_invalid")
            ],
            "note": "Missing seeds are reported, not silently substituted or generated (FR-88).",
        }


def seed_fixture_path(fixture_dir: Path, recipe_id: str) -> Path:
    """The expected fixture file path for a seed."""
    return fixture_dir / "seeds" / f"{recipe_id}.json"


def seed_fixture_mismatches(spec: dict[str, str], recipe: ImportedRecipe) -> list[str]:
    """Compare a validated fixture against the approved seed spec (FR-86).

    The fixture must carry the approved recipe ID, canonical source URL,
    source name/tier, protein class, and the enabled/regular/base-weight-1
    state, plus the content the approved interpretation requires. Any
    mismatch is returned (the fixture is rejected; nothing is substituted).
    """
    mismatches: list[str] = []
    if recipe.recipe_id != spec["recipe_id"]:
        mismatches.append(
            f"recipe_id {recipe.recipe_id!r} is not the approved {spec['recipe_id']!r}"
        )
    if recipe.title != spec["title"]:
        mismatches.append(f"title {recipe.title!r} is not the approved {spec['title']!r}")
    if recipe.reference_url != spec["reference_url"]:
        mismatches.append("reference_url does not match the approved canonical source")
    if recipe.source_name != spec["source_name"]:
        mismatches.append(
            f"source_name {recipe.source_name!r} is not the approved {spec['source_name']!r}"
        )
    if recipe.source_tier != spec["source_tier"]:
        mismatches.append(
            f"source_tier {recipe.source_tier!r} is not the approved {spec['source_tier']!r}"
        )
    if recipe.protein_class != spec["protein_class"]:
        mismatches.append(
            f"protein_class {recipe.protein_class!r} is not the approved {spec['protein_class']!r}"
        )
    if recipe.enabled is not True:
        mismatches.append("the seed must be enabled")
    if recipe.is_regular is not True:
        mismatches.append("the seed must be a regular recipe")
    if recipe.base_weight != 1.0:
        mismatches.append(f"the seed base weight must be 1, got {recipe.base_weight!r}")

    requirements = SEED_CONTENT_REQUIREMENTS.get(spec["recipe_id"], {})
    appliance_types = {appliance.type for appliance in recipe.appliances}
    for needed in requirements.get("required_appliance_types", ()):
        if needed not in appliance_types:
            mismatches.append(f"the approved interpretation requires appliance type {needed!r}")
    ingredient_names = " ".join(ingredient.name.lower() for ingredient in recipe.ingredients)
    for needed in requirements.get("required_ingredient_substrings", ()):
        if needed not in ingredient_names:
            mismatches.append(
                f"the approved interpretation requires an ingredient matching {needed!r}"
            )
    return mismatches


def run_seed_import(paths: GrossyPaths, fixture_dir: Path) -> SeedImportResult:
    """Import the four approved seed recipes from sanitized fixture files.

    For each seed spec:
    1. If the recipe already exists, report "already_exists".
    2. If the fixture file exists, validate it as a ``grossy.recipe.import.v1``
       proposal and apply it.
    3. If the fixture file is missing or invalid, report "controlled missing".

    Never substitutes, generates, or fabricates seed data (FR-88).
    """
    if not paths.database_file.exists():
        raise SeedImportError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    statuses: list[SeedStatus] = []

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            now = _now_utc()
            for spec in SEED_SPECS:
                recipe_id = spec["recipe_id"]

                # Check if already imported
                if recipe_exists(conn, recipe_id):
                    statuses.append(
                        SeedStatus(
                            recipe_id=recipe_id,
                            title=spec["title"],
                            protein_class=spec["protein_class"],
                            status="already_exists",
                            detail="recipe already in the local library",
                        )
                    )
                    continue

                # Look for the fixture file
                fixture_path = seed_fixture_path(fixture_dir, recipe_id)
                if not fixture_path.exists():
                    statuses.append(
                        SeedStatus(
                            recipe_id=recipe_id,
                            title=spec["title"],
                            protein_class=spec["protein_class"],
                            status="missing_fixture",
                            detail=(
                                f"no approved fixture at {fixture_path}; "
                                f"source: {spec['reference_url']}; "
                                f"interpretation: {spec['interpretation']}. "
                                "A controlled later import path is available via "
                                "'grossy recipes import-validate' + 'import-apply' "
                                "with an approved sanitized proposal."
                            ),
                        )
                    )
                    continue

                # Load and validate the fixture
                try:
                    data = json.loads(fixture_path.read_text(encoding="utf-8"))
                    proposal = validate_recipe_import(data)
                except (json.JSONDecodeError, RecipeImportError, OSError) as exc:
                    statuses.append(
                        SeedStatus(
                            recipe_id=recipe_id,
                            title=spec["title"],
                            protein_class=spec["protein_class"],
                            status="fixture_invalid",
                            detail=f"fixture validation failed: {exc}",
                        )
                    )
                    continue

                # Reject fixtures that do not match the approved seed spec:
                # a valid but mismatched fixture must not import the wrong
                # recipe or omit the approved interpretation (FR-86).
                recipe = proposal.recipe
                mismatches = seed_fixture_mismatches(spec, recipe)
                if mismatches:
                    statuses.append(
                        SeedStatus(
                            recipe_id=spec["recipe_id"],
                            title=spec["title"],
                            protein_class=spec["protein_class"],
                            status="fixture_invalid",
                            detail=(
                                "fixture does not match the approved seed spec: "
                                + "; ".join(mismatches)
                            ),
                        )
                    )
                    continue

                # Apply the import within a per-seed savepoint so a storage
                # error (e.g. dangling variant reference) cannot leave a
                # partial recipe row committed (FR-86, FR-88).
                savepoint_name = f"seed_import_{recipe_id.replace('-', '_')}"
                conn.execute(f"SAVEPOINT {savepoint_name}")
                try:
                    _, revision_id = insert_recipe_with_revision(
                        conn,
                        recipe_id=recipe.recipe_id,
                        title=recipe.title,
                        reference_url=recipe.reference_url,
                        source_name=recipe.source_name,
                        source_tier=recipe.source_tier,
                        cuisine=recipe.cuisine,
                        protein_class=recipe.protein_class,
                        total_time_minutes=recipe.total_time_minutes,
                        servings=recipe.servings,
                        steps=list(recipe.steps),
                        ingredients=list(recipe.ingredients),
                        appliances=list(recipe.appliances),
                        variants=list(recipe.variants),
                        base_weight=recipe.base_weight,
                        is_favorite=recipe.is_favorite,
                        is_regular=recipe.is_regular,
                        enabled=recipe.enabled,
                        provenance=SEED_PROVENANCE,
                        now=now,
                    )
                    conn.execute(f"RELEASE {savepoint_name}")
                except RecipeStorageError as exc:
                    conn.execute(f"ROLLBACK TO {savepoint_name}")
                    conn.execute(f"RELEASE {savepoint_name}")
                    statuses.append(
                        SeedStatus(
                            recipe_id=recipe_id,
                            title=spec["title"],
                            protein_class=spec["protein_class"],
                            status="fixture_invalid",
                            detail=f"storage error: {exc}",
                        )
                    )
                    continue

                statuses.append(
                    SeedStatus(
                        recipe_id=recipe_id,
                        title=spec["title"],
                        protein_class=spec["protein_class"],
                        status="imported",
                        detail="imported from approved sanitized fixture",
                        revision_id=revision_id,
                    )
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    imported = sum(1 for s in statuses if s.status == "imported")
    existing = sum(1 for s in statuses if s.status == "already_exists")
    missing = sum(1 for s in statuses if s.status in ("missing_fixture", "fixture_invalid"))

    return SeedImportResult(
        seeds=statuses,
        imported_count=imported,
        existing_count=existing,
        missing_count=missing,
    )


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
