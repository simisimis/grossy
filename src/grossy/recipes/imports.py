"""Recipe import phases (PRD FR-70..FR-75, 10.3; task 07).

Three deterministic, offline phases:

- ``run_import_validate``: the dry-run preview of a
  ``grossy.recipe.import.v1`` proposal (FR-74). State-free: it re-validates
  the file and the checksum binding, and reports exactly what apply would
  persist. No database access, no state change.
- ``run_import_apply``: applies a previewed proposal in one transaction
  (PRD 10.3.4, 10.3.6). It requires the checksum returned by
  ``run_import_validate``: the proposal content is re-verified both against
  its own checksum field and against that accepted preview checksum, so a
  proposal that was never previewed -- or edited and re-hashed after the
  preview -- is refused. Creating an existing recipe is refused: re-import
  is a refresh (task 08), not an import.
- ``run_add_file``: user-authored YAML import without a public URL (FR-75),
  validating the same deterministic invariants as the proposal.

All phases treat file content as untrusted data (PRD 17.4) and never fetch
anything: URL imports operate on the extracted-facts proposal file only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from grossy.config.loader import GrossyConfigError, load_config_file
from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import (
    RECIPE_IMPORT_SCHEMA_NAME,
    RecipeImportError,
    proposal_preview,
    validate_recipe_import,
)
from grossy.recipes.repository import (
    RecipeStorageError,
    insert_recipe_with_revision,
)
from grossy.recipes.yaml_recipe import (
    PROVENANCE_URL_IMPORT,
    PROVENANCE_USER_FILE,
    RecipeYamlError,
    apply_yaml_defaults,
    load_recipe_yaml,
)

_IMPORT_APPLY_SCHEMA = "grossy.recipe.import-apply"
_ADD_FILE_SCHEMA = "grossy.recipe.add-file"


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecipeImportError(f"cannot read proposal {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecipeImportError(f"proposal {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeImportError(f"proposal {path} must be a JSON object")
    return data


@dataclass(frozen=True)
class ImportValidateResult:
    """Dry-run preview of a recipe-import proposal (FR-74)."""

    preview: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.preview


@dataclass(frozen=True)
class ImportApplyResult:
    """Result of applying a previewed recipe-import proposal."""

    recipe_id: str
    revision_id: int
    checksum: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": _IMPORT_APPLY_SCHEMA,
            "schema_version": 1,
            "status": "applied",
            "schema": RECIPE_IMPORT_SCHEMA_NAME,
            "recipe_id": self.recipe_id,
            "revision_id": self.revision_id,
            "revision_number": 1,
            "proposal_checksum": self.checksum,
        }


@dataclass(frozen=True)
class AddFileResult:
    """Result of a user-authored YAML recipe import (FR-75)."""

    recipe_id: str
    revision_id: int
    title: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": _ADD_FILE_SCHEMA,
            "schema_version": 1,
            "status": "applied",
            "schema": "grossy.recipe.yaml.v1",
            "recipe_id": self.recipe_id,
            "revision_id": self.revision_id,
            "revision_number": 1,
            "title": self.title,
        }


def run_import_validate(proposal_path: Path) -> ImportValidateResult:
    """Dry-run preview: validate the proposal file and report its facts.

    State-free and side-effect free (FR-74): the same validation that
    ``import-apply`` runs, minus the persistence, so the preview always
    matches what apply would do.
    """
    data = _load_json_file(proposal_path)
    proposal = validate_recipe_import(data)
    return ImportValidateResult(preview=proposal_preview(proposal))


def run_import_apply(
    proposal_path: Path, preview_checksum: str, paths: GrossyPaths
) -> ImportApplyResult:
    """Apply a previewed recipe-import proposal in one transaction.

    Requires the database; acquires the writer lock; migrates first. The
    proposal content is re-verified against its own checksum field and
    against ``preview_checksum`` -- the checksum returned by
    ``run_import_validate`` (PRD 10.3.6). Only the exact content the caller
    previewed can be applied: a proposal that was never previewed, or edited
    and re-hashed after the preview, is refused before any state change.
    """
    data = _load_json_file(proposal_path)
    proposal = validate_recipe_import(data)
    checksum = proposal.checksum
    recipe = proposal.recipe

    if checksum.lower() != preview_checksum.strip().lower():
        raise RecipeImportError(
            "apply requires the accepted preview checksum; the proposal "
            f"content ({checksum}) does not match the previewed checksum "
            f"({preview_checksum.strip().lower()}). Run 'grossy recipes "
            "import-validate' and apply with the checksum it returned."
        )

    if not paths.database_file.exists():
        raise RecipeImportError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            now = _now_utc()
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
                    provenance=PROVENANCE_URL_IMPORT,
                    now=now,
                )
            except RecipeStorageError as exc:
                raise RecipeImportError(str(exc)) from exc
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return ImportApplyResult(recipe_id=recipe.recipe_id, revision_id=revision_id, checksum=checksum)


def run_add_file(paths: GrossyPaths, recipe_file: Path) -> AddFileResult:
    """Import a user-authored YAML recipe (no public URL required, FR-75).

    The file validates the same deterministic invariants as the URL-import
    proposal; omitted ``servings`` default to the household target from the
    configuration (``household.recipe_servings``).
    """
    try:
        text = recipe_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecipeYamlError(f"cannot read recipe file {recipe_file}: {exc}") from exc
    if not paths.database_file.exists():
        raise RecipeYamlError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    recipe = load_recipe_yaml(text)

    try:
        config = load_config_file(paths.config_file)
    except GrossyConfigError as exc:
        raise RecipeYamlError(f"cannot load configuration for default servings: {exc}") from exc
    recipe = apply_yaml_defaults(recipe, config.household.recipe_servings)

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            now = _now_utc()
            try:
                _, revision_id = insert_recipe_with_revision(
                    conn,
                    recipe_id=recipe.recipe_id,
                    title=recipe.title,
                    reference_url=recipe.reference_url,
                    source_name=cast(str, recipe.source_name),
                    source_tier=cast(str, recipe.source_tier),
                    cuisine=recipe.cuisine,
                    protein_class=recipe.protein_class,
                    total_time_minutes=recipe.total_time_minutes,
                    servings=cast(float, recipe.servings),
                    steps=list(recipe.steps),
                    ingredients=list(recipe.ingredients),
                    appliances=list(recipe.appliances),
                    variants=list(recipe.variants),
                    base_weight=recipe.base_weight,
                    is_favorite=recipe.is_favorite,
                    is_regular=recipe.is_regular,
                    enabled=recipe.enabled,
                    provenance=PROVENANCE_USER_FILE,
                    now=now,
                )
            except RecipeStorageError as exc:
                raise RecipeYamlError(str(exc)) from exc
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return AddFileResult(
        recipe_id=recipe.recipe_id,
        revision_id=revision_id,
        title=recipe.title,
    )
