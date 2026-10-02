"""Recipe refresh: diff preview and apply as a new immutable revision (PRD FR-78..FR-80; task 08).

A refresh updates an existing recipe's accepted content without altering
historical revision snapshots. The ``grossy.recipe.refresh.v1`` proposal
carries the complete new revision content. The diff phase compares the
proposal against the current accepted revision and reports every field
change. The apply phase creates a new immutable revision (next sequence
number), updates the recipe's current pointer, and retains all prior
revisions byte-for-byte (FR-79, FR-80).

Like imports, the refresh proposal is checksum-bound: apply requires the
exact checksum returned by the diff preview (PRD 10.3.6).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)

from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import MigrationChecksumError, run_migrations
from grossy.db.snapshot import SnapshotError
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import (
    _HEX_CHECKSUM_RE,
    MAX_APPLIANCES,
    MAX_CUISINE_LENGTH,
    MAX_INGREDIENTS,
    MAX_SOURCE_NAME_LENGTH,
    MAX_SOURCED_STEP_LENGTH,
    MAX_STEPS,
    MAX_TITLE_LENGTH,
    MAX_VARIANTS,
    PROTEIN_CLASSES,
    RECIPE_ID_PATTERN,
    SOURCE_TIERS,
    ApplianceRequirement,
    RecipeIngredient,
    RecipeVariantLink,
    _check_generated_at,
    _check_positive_finite,
    _format_validation_error,
    proposal_checksum,
    sanitize_single_line,
    validate_reference_url,
)
from grossy.recipes.repository import (
    RecipeStorageError,
    get_recipe_row,
    get_revision_snapshot,
    insert_revision_for_existing_recipe,
)
from grossy.security import ProhibitedContentError, scan_prohibited_content

REFRESH_SCHEMA_NAME = "grossy.recipe.refresh.v1"
REFRESH_SCHEMA_VERSION = 1

PROVENANCE_URL_REFRESH = "url-refresh"


class RecipeRefreshError(ValueError):
    """Raised when a recipe-refresh proposal fails deterministic validation."""


# --- refresh payload model -----------------------------------------------------


class RecipeRefreshPayload(BaseModel):
    """The new-revision content of ``grossy.recipe.refresh.v1``.

    Carries the complete revision data (ingredients, steps, appliances,
    variants) plus the recipe-level fields that may change on refresh
    (title, source, cuisine, protein class, total time).
    """

    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(pattern=RECIPE_ID_PATTERN)
    title: str = Field(min_length=1)
    reference_url: str | None = None
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
    provenance: str = PROVENANCE_URL_REFRESH

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
    def _check_source_name(cls, value: str) -> str:
        return sanitize_single_line(value, "source_name", MAX_SOURCE_NAME_LENGTH)

    @field_validator("source_tier")
    @classmethod
    def _check_source_tier(cls, value: str) -> str:
        if value not in SOURCE_TIERS:
            raise RecipeRefreshError(
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
            raise RecipeRefreshError(
                f"protein_class must be one of {', '.join(PROTEIN_CLASSES)}, got {value!r}"
            )
        return value

    @field_validator("servings")
    @classmethod
    def _check_servings(cls, value: float) -> float:
        return _check_positive_finite(value, "servings")

    @field_validator("steps")
    @classmethod
    def _check_steps(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= MAX_STEPS:
            raise RecipeRefreshError(
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
            raise RecipeRefreshError(
                f"a recipe must have between 1 and {MAX_INGREDIENTS} ingredients, got {len(value)}"
            )
        return value

    @field_validator("appliances")
    @classmethod
    def _check_appliances(cls, value: list[ApplianceRequirement]) -> list[ApplianceRequirement]:
        if len(value) > MAX_APPLIANCES:
            raise RecipeRefreshError(
                f"a recipe may have at most {MAX_APPLIANCES} appliance requirements, "
                f"got {len(value)}"
            )
        return value

    @field_validator("variants")
    @classmethod
    def _check_variants(cls, value: list[RecipeVariantLink]) -> list[RecipeVariantLink]:
        if len(value) > MAX_VARIANTS:
            raise RecipeRefreshError(
                f"a recipe may have at most {MAX_VARIANTS} variant links, got {len(value)}"
            )
        seen: set[tuple[str, str]] = set()
        for variant in value:
            key = (variant.linked_recipe_id, variant.variant_type)
            if key in seen:
                raise RecipeRefreshError(
                    f"duplicate variant link {variant.linked_recipe_id!r}/{variant.variant_type!r}"
                )
            seen.add(key)
        return value

    @field_validator("provenance")
    @classmethod
    def _check_provenance(cls, value: str) -> str:
        return sanitize_single_line(value, "provenance", 100)


# --- versioned envelope (grossy.recipe.refresh.v1) -------------------------------


class RecipeRefreshProposal(BaseModel):
    """Versioned, schema-bound recipe-refresh proposal (PRD 11)."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str
    schema_version: StrictInt
    generated_at: str
    payload: RecipeRefreshPayload
    checksum: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str) -> str:
        if value != REFRESH_SCHEMA_NAME:
            raise RecipeRefreshError(f"schema_name must be {REFRESH_SCHEMA_NAME!r}, got {value!r}")
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != REFRESH_SCHEMA_VERSION:
            raise RecipeRefreshError(
                f"schema_version must be {REFRESH_SCHEMA_VERSION}, got {value}"
            )
        return value

    @field_validator("generated_at")
    @classmethod
    def _check_generated_at(cls, value: str) -> str:
        _check_generated_at(value)
        return value


# --- public validation (fail closed) --------------------------------------------


def validate_recipe_refresh(data: dict[str, Any]) -> RecipeRefreshProposal:
    """Validate a raw recipe-refresh proposal dict; fail closed on any violation."""
    try:
        scan_prohibited_content(data)
    except ProhibitedContentError as exc:
        raise RecipeRefreshError(str(exc)) from exc

    expected_keys = {"schema_name", "schema_version", "generated_at", "payload", "checksum"}
    unknown = sorted(set(data) - expected_keys)
    if unknown:
        raise RecipeRefreshError(
            f"unknown field(s) in recipe-refresh proposal: {', '.join(unknown)}"
        )
    missing = sorted(expected_keys - set(data))
    if missing:
        raise RecipeRefreshError(
            f"missing field(s) in recipe-refresh proposal: {', '.join(missing)}"
        )

    schema_name = data["schema_name"]
    if schema_name != REFRESH_SCHEMA_NAME:
        raise RecipeRefreshError(
            f"schema_name must be {REFRESH_SCHEMA_NAME!r}, got {schema_name!r}"
        )
    schema_version = data["schema_version"]
    if schema_version != REFRESH_SCHEMA_VERSION:
        raise RecipeRefreshError(
            f"schema_version must be {REFRESH_SCHEMA_VERSION}, got {schema_version}"
        )
    generated_at = data["generated_at"]
    if not isinstance(generated_at, str):
        raise RecipeRefreshError(f"generated_at must be a string, got {generated_at!r}")
    _check_generated_at(generated_at)

    checksum = data["checksum"]
    if not isinstance(checksum, str) or not _HEX_CHECKSUM_RE.fullmatch(checksum):
        raise RecipeRefreshError(f"checksum must be a 64-character hex string, got {checksum!r}")
    payload = {key: value for key, value in data.items() if key != "checksum"}
    if proposal_checksum(payload) != checksum.lower():
        raise RecipeRefreshError(
            "checksum does not match the proposal content; refusing an "
            "unpreviewed or modified proposal"
        )

    try:
        return RecipeRefreshProposal.model_validate(data)
    except ValidationError as exc:
        raise RecipeRefreshError(
            f"recipe-refresh proposal validation failed: {_format_validation_error(exc)}"
        ) from exc


# --- diff preview ----------------------------------------------------------------


_INGREDIENT_FIELDS = (
    "amount",
    "unit",
    "pantry_eligible",
    "indivisible",
    "package_unit",
    "package_size",
    "notes",
)


def _ingredient_record(ing: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Canonical multiset identity for an ingredient row.

    The normalized name plus the values of every compared field, so two
    ingredient rows are treated as the same element only when all fields
    match (FR-78: every persisted change must appear in the diff).
    """
    return (ing["name"], tuple(ing.get(field) for field in _INGREDIENT_FIELDS))


def _compare_ingredients(
    old: list[dict[str, Any]], new: list[RecipeIngredient]
) -> list[dict[str, Any]]:
    """Compare ingredient lists as a multiset, reporting every change.

    Ingredients are matched by (normalized) name with multiplicity rather than
    collapsing into a dictionary keyed by name, so duplicate names — which the
    import schema permits and which an existing revision may already contain —
    are handled accurately instead of hiding an applied change (FR-78).

    For a name appearing exactly once on each side, a precise field-level
    ``changed`` entry is reported. Otherwise the multiset difference is
    reported as ``removed`` (old-only) and ``added`` (new-only) entries after
    matching identical full records, so a duplicate-only change is still
    visible and every persisted change appears in the diff.
    """
    new_dumps = [ing.model_dump() for ing in new]

    old_by_name: dict[str, list[dict[str, Any]]] = {}
    for rec in old:
        old_by_name.setdefault(rec["name"], []).append(rec)
    new_by_name: dict[str, list[dict[str, Any]]] = {}
    for rec in new_dumps:
        new_by_name.setdefault(rec["name"], []).append(rec)

    changes: list[dict[str, Any]] = []
    for name in sorted(set(old_by_name) | set(new_by_name)):
        old_recs = old_by_name.get(name, [])
        new_recs = new_by_name.get(name, [])

        # Single occurrence on each side: report a precise field-level change.
        if len(old_recs) == 1 and len(new_recs) == 1:
            field_diffs: dict[str, dict[str, Any]] = {}
            for key in _INGREDIENT_FIELDS:
                if old_recs[0].get(key) != new_recs[0].get(key):
                    field_diffs[key] = {
                        "old": old_recs[0].get(key),
                        "new": new_recs[0].get(key),
                    }
            if field_diffs:
                changes.append({"action": "changed", "name": name, "fields": field_diffs})
            continue

        # Multiplicity case: match each new record to an identical old record
        # (at most once each). Unmatched new records are "added"; the leftover
        # old records are "removed". This is the exact multiset difference.
        unmatched_old = list(old_recs)
        for new_rec in new_recs:
            for i, old_rec in enumerate(unmatched_old):
                if _ingredient_record(old_rec) == _ingredient_record(new_rec):
                    unmatched_old.pop(i)
                    break
            else:
                changes.append({"action": "added", "name": name, "new": new_rec})
        for old_rec in unmatched_old:
            changes.append({"action": "removed", "name": name, "old": old_rec})

    return changes


def _appliance_record(rec: dict[str, Any]) -> tuple[Any, ...]:
    """Canonical multiset identity for an appliance requirement row."""
    return (
        rec["type"],
        rec["capability"],
        rec.get("model_constraint"),
        rec.get("capacity_constraint"),
        rec.get("capacity_unit"),
    )


def _appliance_record_model(a: ApplianceRequirement) -> tuple[Any, ...]:
    """Canonical multiset identity for an appliance requirement model."""
    return (a.type, a.capability, a.model_constraint, a.capacity_constraint, a.capacity_unit)


def _compare_appliances(
    old: list[dict[str, Any]], new: list[ApplianceRequirement]
) -> list[dict[str, Any]]:
    """Compare appliance requirement lists as a multiset, reporting every change.

    Appliances are matched by ``(type, capability)`` with multiplicity and
    all constraint fields included in the identity, so duplicate keys are
    handled accurately rather than collapsing into a single entry (FR-78).

    For a (type, capability) pair appearing exactly once on each side, a
    precise field-level ``changed`` entry is reported. Otherwise the multiset
    difference is reported as ``removed`` (old-only) and ``added`` (new-only)
    entries after matching identical full records.
    """
    old_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in old:
        old_by_key.setdefault((rec["type"], rec["capability"]), []).append(rec)
    new_by_key: dict[tuple[str, str], list[ApplianceRequirement]] = {}
    for new_rec in new:
        key = (new_rec.type, new_rec.capability)
        if key not in new_by_key:
            new_by_key[key] = []
        new_by_key[key].append(new_rec)

    changes: list[dict[str, Any]] = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        old_recs = old_by_key.get(key, [])
        new_recs = new_by_key.get(key, [])

        # Single occurrence on each side: report a precise field-level change.
        if len(old_recs) == 1 and len(new_recs) == 1:
            field_diffs: dict[str, dict[str, Any]] = {}
            for field in ("model_constraint", "capacity_constraint", "capacity_unit"):
                old_val = old_recs[0].get(field)
                new_val = getattr(new_recs[0], field)
                if old_val != new_val:
                    field_diffs[field] = {"old": old_val, "new": new_val}
            if field_diffs:
                changes.append(
                    {
                        "action": "changed",
                        "type": key[0],
                        "capability": key[1],
                        "fields": field_diffs,
                    }
                )
            continue

        # Multiplicity case: match each new record to an identical old record.
        unmatched_old = list(old_recs)
        for new_rec in new_recs:
            for i, old_rec in enumerate(unmatched_old):
                if _appliance_record(old_rec) == _appliance_record_model(new_rec):
                    unmatched_old.pop(i)
                    break
            else:
                changes.append(
                    {
                        "action": "added",
                        "type": key[0],
                        "capability": key[1],
                        "new": new_rec.model_dump(),
                    }
                )
        for old_rec in unmatched_old:
            changes.append(
                {
                    "action": "removed",
                    "type": key[0],
                    "capability": key[1],
                    "old": old_rec,
                }
            )

    return changes


def _compare_variants(
    old: list[dict[str, Any]], new: list[RecipeVariantLink]
) -> list[dict[str, Any]]:
    """Compare variant link lists."""
    old_set = {(v["linked_recipe_id"], v["variant_type"]) for v in old}
    new_set = {(v.linked_recipe_id, v.variant_type) for v in new}
    changes: list[dict[str, Any]] = []
    for key in old_set - new_set:
        changes.append({"action": "removed", "linked_recipe_id": key[0], "variant_type": key[1]})
    for key in new_set - old_set:
        changes.append({"action": "added", "linked_recipe_id": key[0], "variant_type": key[1]})
    return changes


@dataclass(frozen=True)
class RefreshDiffResult:
    """Diff preview of a recipe-refresh proposal (FR-78)."""

    diff: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.diff


def run_refresh_diff(proposal_path: Path) -> RefreshDiffResult:
    """Produce a diff preview comparing the refresh proposal to the current revision.

    Requires the database (to read the current revision) but performs no
    state change. The diff lists every field that would change, giving the
    user a complete preview before apply (FR-78). The preview carries the
    validated proposal's checksum so the user can apply with exactly the
    checksum the preview returned.
    """
    data = _load_json_file(proposal_path)
    proposal = validate_recipe_refresh(data)

    # Read the current accepted revision from the database.
    # We use a read-only connection: no lock needed for a read.
    # The caller must have a database; we check here.
    return _compute_diff(proposal, _find_db_for_diff())


def _find_db_for_diff() -> Path:
    """Resolve the database path for diff (requires a resolved path).

    The diff command needs to read the current revision. We use the
    default path resolution. This is state-free: no lock, no write.
    """
    from grossy.paths import resolve_paths

    return resolve_paths().database_file


def run_refresh_diff_with_paths(proposal_path: Path, paths: GrossyPaths) -> RefreshDiffResult:
    """Produce a diff preview using explicit paths (for testability).

    Persists the preview state so that ``run_refresh_apply`` can verify a
    preview occurred (FR-78). Without a persisted preview, apply is rejected.

    Runs pending migrations under the writer lock before querying the
    current revision so that databases created before newer migrations
    (e.g. schema-v6 without the revision-snapshot identity columns) can
    be previewed without a separate upgrade step (PRD 12.2.4).

    Migration failures are surfaced through the controlled recipe error
    path with the backup location and failure cause (PRD 18).
    """
    data = _load_json_file(proposal_path)
    proposal = validate_recipe_refresh(data)

    if not paths.database_file.exists():
        raise RecipeRefreshError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    with WriterLock(paths.state_dir):
        _run_migrations_controlled(paths)
        result = _compute_diff(proposal, paths.database_file)
        # Persist the preview state inside the lock, bound to the exact
        # current revision that was diffed, so that apply can detect if the
        # revision has changed between preview and apply (stale preview).
        current_rev_id = cast(int, result.diff["current_revision_id"])
        _persist_preview_state(
            proposal.payload.recipe_id, proposal.checksum, paths.state_dir, current_rev_id
        )
    return result


def _compute_diff(proposal: RecipeRefreshProposal, db_path: Path) -> RefreshDiffResult:
    """Compute the diff between the proposal and the current accepted revision.

    The preview carries the validated proposal's checksum: ``apply`` accepts
    exactly the checksum the preview returned, so the documented
    diff-then-apply flow works end to end.
    """
    payload = proposal.payload
    conn = open_connection(db_path)
    try:
        recipe_row = get_recipe_row(conn, payload.recipe_id)
        if recipe_row is None:
            raise RecipeRefreshError(
                f"recipe {payload.recipe_id!r} does not exist; "
                "refresh requires an existing recipe (use import for new recipes)"
            )
        current_revision_id = cast(int, recipe_row["current_revision_id"])
        current_snapshot = get_revision_snapshot(conn, current_revision_id)
    except RecipeStorageError as exc:
        raise RecipeRefreshError(str(exc)) from exc
    finally:
        conn.close()

    # Build the diff
    changes: dict[str, Any] = {}

    # Recipe-level field changes
    recipe_field_diffs: dict[str, dict[str, Any]] = {}
    if recipe_row["title"] != payload.title:
        recipe_field_diffs["title"] = {
            "old": recipe_row["title"],
            "new": payload.title,
        }
    if (recipe_row["reference_url"] or None) != payload.reference_url:
        recipe_field_diffs["reference_url"] = {
            "old": recipe_row["reference_url"],
            "new": payload.reference_url,
        }
    if recipe_row["source_name"] != payload.source_name:
        recipe_field_diffs["source_name"] = {
            "old": recipe_row["source_name"],
            "new": payload.source_name,
        }
    if recipe_row["source_tier"] != payload.source_tier:
        recipe_field_diffs["source_tier"] = {
            "old": recipe_row["source_tier"],
            "new": payload.source_tier,
        }
    if (recipe_row["cuisine"] or None) != payload.cuisine:
        recipe_field_diffs["cuisine"] = {
            "old": recipe_row["cuisine"],
            "new": payload.cuisine,
        }
    if recipe_row["protein_class"] != payload.protein_class:
        recipe_field_diffs["protein_class"] = {
            "old": recipe_row["protein_class"],
            "new": payload.protein_class,
        }
    if (recipe_row["total_time_minutes"] or None) != payload.total_time_minutes:
        recipe_field_diffs["total_time_minutes"] = {
            "old": recipe_row["total_time_minutes"],
            "new": payload.total_time_minutes,
        }
    if recipe_field_diffs:
        changes["recipe_fields"] = recipe_field_diffs

    # Revision-level changes
    if current_snapshot["servings"] != payload.servings:
        changes["servings"] = {
            "old": current_snapshot["servings"],
            "new": payload.servings,
        }
    if (current_snapshot["total_time_minutes"] or None) != payload.total_time_minutes:
        changes["total_time_minutes"] = {
            "old": current_snapshot["total_time_minutes"],
            "new": payload.total_time_minutes,
        }
    if current_snapshot["steps"] != payload.steps:
        changes["steps"] = {
            "old_count": len(current_snapshot["steps"]),
            "new_count": len(payload.steps),
            "old": current_snapshot["steps"],
            "new": payload.steps,
        }
    if current_snapshot.get("provenance") != payload.provenance:
        changes["provenance"] = {
            "old": current_snapshot.get("provenance"),
            "new": payload.provenance,
        }

    ingredient_changes = _compare_ingredients(current_snapshot["ingredients"], payload.ingredients)
    if ingredient_changes:
        changes["ingredients"] = ingredient_changes

    appliance_changes = _compare_appliances(current_snapshot["appliances"], payload.appliances)
    if appliance_changes:
        changes["appliances"] = appliance_changes

    variant_changes = _compare_variants(current_snapshot["variants"], payload.variants)
    if variant_changes:
        changes["variants"] = variant_changes

    checksum = proposal.checksum
    current_rev_num = _get_revision_number(
        current_snapshot.get("revision_id"), db_path, payload.recipe_id
    )
    next_rev_num = _get_next_revision_number(db_path, payload.recipe_id)
    diff_payload: dict[str, object] = {
        "schema_name": "grossy.recipe.refresh-diff",
        "schema_version": 1,
        "status": "preview",
        "schema": REFRESH_SCHEMA_NAME,
        "proposal_checksum": checksum,
        "recipe_id": payload.recipe_id,
        "current_revision_id": current_revision_id,
        "current_revision_number": current_rev_num,
        "next_revision_number": next_rev_num,
        "has_changes": bool(changes),
        "changes": changes,
    }
    return RefreshDiffResult(diff=diff_payload)


def _get_revision_number(revision_id: int | None, db_path: Path, recipe_id: str) -> int:
    if revision_id is None:
        return _get_next_revision_number(db_path, recipe_id) - 1
    conn = open_connection(db_path)
    try:
        row = conn.execute(
            "SELECT revision_number FROM recipe_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        return cast(int, row[0]) if row else 1
    finally:
        conn.close()


def _get_next_revision_number(db_path: Path, recipe_id: str) -> int:
    conn = open_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(revision_number) FROM recipe_revisions r "
            "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        max_num = row[0] if row and row[0] is not None else 0
        return max_num + 1
    finally:
        conn.close()


# --- apply ----------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshApplyResult:
    """Result of applying a recipe refresh as a new revision."""

    recipe_id: str
    revision_id: int
    revision_number: int
    checksum: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": "grossy.recipe.refresh-apply",
            "schema_version": 1,
            "status": "applied",
            "schema": REFRESH_SCHEMA_NAME,
            "recipe_id": self.recipe_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "proposal_checksum": self.checksum,
        }


def run_refresh_apply(
    proposal_path: Path, preview_checksum: str, paths: GrossyPaths
) -> RefreshApplyResult:
    """Apply a previewed recipe-refresh proposal as a new immutable revision.

    Requires a persisted preview state (written by ``run_refresh_diff_with_paths``)
    so that a direct CLI caller cannot bypass the mandatory preview phase
    (FR-78). Creates a new revision with the next sequence number, updates
    the recipe's ``current_revision_id``, and retains all prior revisions
    (FR-79, FR-80). Historical planner runs continue referencing their
    exact revision snapshots.
    """
    data = _load_json_file(proposal_path)
    proposal = validate_recipe_refresh(data)
    checksum = proposal.checksum
    payload = proposal.payload

    if checksum.lower() != preview_checksum.strip().lower():
        raise RecipeRefreshError(
            "apply requires the accepted preview checksum; the proposal "
            f"content ({checksum}) does not match the previewed checksum "
            f"({preview_checksum.strip().lower()}). Run 'grossy recipes "
            "refresh-diff' and apply with the checksum it returned."
        )

    if not paths.database_file.exists():
        raise RecipeRefreshError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    with WriterLock(paths.state_dir):
        _run_migrations_controlled(paths)

        # FR-78: verify a preview was actually performed for this recipe+checksum.
        # Read the preview state inside the writer lock so that the check and
        # the apply are atomic with respect to other writers. The preview is
        # bound to the exact current revision: if another refresh has been
        # applied since the preview, the current revision will differ and the
        # stale apply is rejected.
        preview_state = _read_preview_state(payload.recipe_id, paths.state_dir)
        if preview_state is None or preview_state.get("checksum", "").lower() != checksum.lower():
            raise RecipeRefreshError(
                "apply requires a prior 'grossy recipes refresh-diff' preview for "
                f"{payload.recipe_id!r}; no matching persisted preview state found. "
                "Run 'grossy recipes refresh-diff' first (FR-78)."
            )

        conn = open_connection(paths.database_file)
        try:
            # Verify the recipe exists
            recipe_row = get_recipe_row(conn, payload.recipe_id)
            if recipe_row is None:
                raise RecipeRefreshError(
                    f"recipe {payload.recipe_id!r} does not exist; "
                    "refresh requires an existing recipe"
                )

            # Reject stale apply: if the preview was taken against a different
            # revision (i.e. another refresh was applied after the preview),
            # the preview is no longer valid and must be re-done.
            previewed_rev_id = preview_state.get("current_revision_id")
            if previewed_rev_id is None:
                raise RecipeRefreshError(
                    "preview state is missing the bound revision ID; "
                    "run 'grossy recipes refresh-diff' again to re-preview."
                )
            current_rev_id = cast(int, recipe_row["current_revision_id"])
            if previewed_rev_id != current_rev_id:
                raise RecipeRefreshError(
                    f"stale preview: the preview was taken against revision "
                    f"{previewed_rev_id} but the current revision is "
                    f"{current_rev_id}. The recipe has changed since the "
                    "preview; run 'grossy recipes refresh-diff' again."
                )

            now = _now_utc()
            revision_id, revision_number = insert_revision_for_existing_recipe(
                conn,
                recipe_id=payload.recipe_id,
                title=payload.title,
                reference_url=payload.reference_url,
                source_name=payload.source_name,
                source_tier=payload.source_tier,
                cuisine=payload.cuisine,
                protein_class=payload.protein_class,
                total_time_minutes=payload.total_time_minutes,
                servings=payload.servings,
                steps=list(payload.steps),
                ingredients=list(payload.ingredients),
                appliances=list(payload.appliances),
                variants=list(payload.variants),
                provenance=payload.provenance,
                now=now,
            )
            conn.commit()
        except RecipeStorageError as exc:
            raise RecipeRefreshError(str(exc)) from exc
        except RecipeRefreshError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return RefreshApplyResult(
        recipe_id=payload.recipe_id,
        revision_id=revision_id,
        revision_number=revision_number,
        checksum=checksum,
    )


# --- migration error control (PRD 18) ---------------------------------------------


def _run_migrations_controlled(paths: GrossyPaths) -> None:
    """Run migrations and surface failures through the controlled recipe error path.

    Catches migration-checksum, snapshot-verification, SQLite, and OS-level
    errors that ``run_migrations`` can raise and re-raises them as
    ``RecipeRefreshError`` with the failure cause (PRD 18).

    The reported restore point is bound to *this* migration invocation: it is
    the backup that ``run_migrations`` created and verified during the attempt
    (carried on the exception as ``verified_backup_path``). A checksum mismatch
    aborts before any backup exists and a failed backup verification leaves no
    *verified* backup, so both report that no verified backup is available for
    the attempt. This avoids advertising an unrelated stale backup (e.g. one
    from a prior ``init``/import) or an unverified file as the recovery point.
    The database is preserved: a failed migration is rolled back
    transactionally by the runner.
    """
    try:
        run_migrations(paths.database_file, paths.backups_dir)
    except (MigrationChecksumError, SnapshotError, sqlite3.DatabaseError, OSError) as exc:
        verified_backup = getattr(exc, "verified_backup_path", None)
        if isinstance(verified_backup, Path):
            raise RecipeRefreshError(
                f"migration failure: {exc}\n"
                f"database is preserved (transactional rollback).\n"
                f"backup: {verified_backup}"
            ) from exc
        raise RecipeRefreshError(
            f"migration failure: {exc}\n"
            f"database is preserved. No verified backup is available for this "
            f"attempt (the failure occurred before a backup was verified, or "
            f"backup verification failed)."
        ) from exc


# --- helpers ----------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- preview state persistence (FR-78) --------------------------------------------


def _preview_state_path(recipe_id: str, state_dir: Path) -> Path:
    return state_dir / "refresh_previews" / f"{recipe_id}.json"


def _persist_preview_state(
    recipe_id: str, checksum: str, state_dir: Path, current_revision_id: int
) -> None:
    """Persist the preview state so apply can verify a preview occurred (FR-78).

    The state is bound to the exact revision that was previewed, so a stale
    apply (after another revision was accepted) is detected and rejected.

    Written under the PRD section 13 permission policy: the preview directory
    is enforced to 0700 and the file is created 0600 before the atomic rename
    publishes it (including when replacing a prior preview file), so the state
    is never group- or world-readable even under a permissive umask.
    """
    path = _preview_state_path(recipe_id, state_dir)
    ensure_private_dir(path.parent)
    atomic_write_text(
        path,
        json.dumps(
            {
                "recipe_id": recipe_id,
                "checksum": checksum,
                "current_revision_id": current_revision_id,
                "previewed_at": _now_utc(),
            },
            indent=2,
        ),
    )


def _read_preview_state(recipe_id: str, state_dir: Path) -> dict[str, Any] | None:
    """Read the persisted preview state, or None if no preview was recorded."""
    path = _preview_state_path(recipe_id, state_dir)
    if not path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return None


# --- helpers ----------------------------------------------------------------------


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecipeRefreshError(f"cannot read proposal {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecipeRefreshError(f"proposal {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeRefreshError(f"proposal {path} must be a JSON object")
    return data
