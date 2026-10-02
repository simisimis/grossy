"""Revisioned recipe storage (PRD FR-70..FR-72, FR-79, FR-80; task 07).

Recipes are stored offline in SQLite and remain usable without refetching
their source (FR-70). Every accepted import creates an immutable revision:
the ``recipe_revisions`` row, its ``recipe_ingredients`` rows, and its
``recipe_appliance_requirements`` rows are insert-only and are never
updated or deleted (PRD 12.1.12). ``run_recipe_slots`` references a
``recipe_revisions`` id, so historical runs retain their exact revision
snapshots (FR-80).

This module is the only write path for revision data: it exposes insert and
read functions only. There is deliberately no update or delete API for
revisions, ingredients, or appliance requirements.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from grossy.recipes.proposal import ApplianceRequirement, RecipeIngredient, RecipeVariantLink


class RecipeStorageError(Exception):
    """Raised when a recipe storage invariant is violated."""


def _get_recipe_row_id(conn: sqlite3.Connection, recipe_id: str) -> int | None:
    row = conn.execute("SELECT id FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
    return cast(int, row[0]) if row is not None else None


def recipe_exists(conn: sqlite3.Connection, recipe_id: str) -> bool:
    return _get_recipe_row_id(conn, recipe_id) is not None


def get_recipe_row(conn: sqlite3.Connection, recipe_id: str) -> dict[str, Any] | None:
    """Return the stable recipe identity row, or None when unknown."""
    row = conn.execute(
        "SELECT id, recipe_id, title, reference_url, source_name, source_tier, cuisine, "
        "protein_class, total_time_minutes, current_revision_id, base_weight, is_favorite, "
        "is_regular, enabled, created_at, updated_at "
        "FROM recipes WHERE recipe_id = ?",
        (recipe_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": cast(int, row[0]),
        "recipe_id": cast(str, row[1]),
        "title": cast(str, row[2]),
        "reference_url": row[3],
        "source_name": row[4],
        "source_tier": row[5],
        "cuisine": row[6],
        "protein_class": cast(str, row[7]),
        "total_time_minutes": row[8],
        "current_revision_id": row[9],
        "base_weight": cast(float, row[10]),
        "is_favorite": bool(row[11]),
        "is_regular": bool(row[12]),
        "enabled": bool(row[13]),
        "created_at": cast(str, row[14]),
        "updated_at": cast(str, row[15]),
    }


def insert_recipe_with_revision(
    conn: sqlite3.Connection,
    *,
    recipe_id: str,
    title: str,
    reference_url: str | None,
    source_name: str,
    source_tier: str,
    cuisine: str | None,
    protein_class: str,
    total_time_minutes: int | None,
    servings: float,
    steps: list[str],
    ingredients: list[RecipeIngredient],
    appliances: list[ApplianceRequirement],
    variants: list[RecipeVariantLink],
    base_weight: float,
    is_favorite: bool,
    is_regular: bool,
    enabled: bool,
    provenance: str,
    now: str,
) -> tuple[int, int]:
    """Create a new recipe with its first immutable revision.

    Must run inside the caller's open transaction; raises
    :class:`RecipeStorageError` (transaction rolls back) when the recipe ID
    already exists or a variant link references an unknown recipe. Returns
    ``(recipe_row_id, revision_row_id)``.
    """
    if recipe_exists(conn, recipe_id):
        raise RecipeStorageError(
            f"recipe {recipe_id!r} already exists; importing an existing recipe "
            "is a refresh (grossy recipes refresh), not an import"
        )

    recipe_row_id = cast(
        int,
        conn.execute(
            "INSERT INTO recipes (recipe_id, title, reference_url, source_name, "
            "source_tier, cuisine, protein_class, total_time_minutes, "
            "current_revision_id, base_weight, is_favorite, is_regular, enabled, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                recipe_id,
                title,
                reference_url,
                source_name,
                source_tier,
                cuisine,
                protein_class,
                total_time_minutes,
                base_weight,
                1 if is_favorite else 0,
                1 if is_regular else 0,
                1 if enabled else 0,
                now,
                now,
            ),
        ).lastrowid,
    )

    for variant in variants:
        if _get_recipe_row_id(conn, variant.linked_recipe_id) is None:
            raise RecipeStorageError(
                f"variant link references unknown recipe "
                f"{variant.linked_recipe_id!r}; import the linked recipe first"
            )

    revision_row_id = cast(
        int,
        conn.execute(
            "INSERT INTO recipe_revisions (recipe_id, revision_number, source_name, "
            "reference_url, cuisine, servings, total_time_minutes, steps_json, "
            "provenance, accepted_at, created_at, title, source_tier, protein_class) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recipe_row_id,
                source_name,
                reference_url,
                cuisine,
                servings,
                total_time_minutes,
                json.dumps(steps, ensure_ascii=False),
                provenance,
                now,
                now,
                title,
                source_tier,
                protein_class,
            ),
        ).lastrowid,
    )

    conn.execute(
        "UPDATE recipes SET current_revision_id = ? WHERE id = ?",
        (revision_row_id, recipe_row_id),
    )

    for ingredient in ingredients:
        conn.execute(
            "INSERT INTO recipe_ingredients (revision_id, ingredient_name, amount, "
            "unit, pantry_eligible, indivisible, package_unit, package_size, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_row_id,
                ingredient.name,
                ingredient.amount,
                ingredient.unit,
                1 if ingredient.pantry_eligible else 0,
                1 if ingredient.indivisible else 0,
                ingredient.package_unit,
                ingredient.package_size,
                ingredient.notes,
            ),
        )

    for appliance in appliances:
        conn.execute(
            "INSERT INTO recipe_appliance_requirements (revision_id, appliance_type, "
            "capability, model_constraint, capacity_constraint, capacity_unit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                revision_row_id,
                appliance.type,
                appliance.capability,
                appliance.model_constraint,
                appliance.capacity_constraint,
                appliance.capacity_unit,
            ),
        )

    # Variant links are revision-bound: each link belongs to the revision that
    # declared it, so a historical snapshot can retain the exact relationships
    # of that revision (FR-79, FR-80).
    for variant in variants:
        conn.execute(
            "INSERT INTO recipe_variants (revision_id, linked_recipe_id, variant_type, "
            "created_at) VALUES (?, ?, ?, ?)",
            (
                revision_row_id,
                _get_recipe_row_id(conn, variant.linked_recipe_id),
                variant.variant_type,
                now,
            ),
        )

    return recipe_row_id, revision_row_id


def list_revisions(conn: sqlite3.Connection, recipe_id: str) -> list[dict[str, Any]]:
    """Return every accepted revision of a recipe in ascending order.

    The full history is always retained: later imports of other recipes or
    maintenance events never alter or remove prior revisions (FR-79, FR-80).
    """
    rows = conn.execute(
        "SELECT r.id, r.revision_number, r.source_name, r.reference_url, r.cuisine, "
        "r.servings, r.total_time_minutes, r.steps_json, r.provenance, r.accepted_at, "
        "r.created_at, r.title, r.source_tier, r.protein_class "
        "FROM recipe_revisions r JOIN recipes p ON p.id = r.recipe_id "
        "WHERE p.recipe_id = ? ORDER BY r.revision_number",
        (recipe_id,),
    ).fetchall()
    return [
        {
            "id": cast(int, row[0]),
            "revision_number": cast(int, row[1]),
            "source_name": row[2],
            "reference_url": row[3],
            "cuisine": row[4],
            "servings": cast(float, row[5]),
            "total_time_minutes": row[6],
            "steps": json.loads(cast(str, row[7])),
            "provenance": row[8],
            "accepted_at": cast(str, row[9]),
            "created_at": cast(str, row[10]),
            "title": row[11],
            "source_tier": row[12],
            "protein_class": row[13],
        }
        for row in rows
    ]


def get_revision_snapshot(conn: sqlite3.Connection, revision_id: int) -> dict[str, Any]:
    """Return the exact persisted snapshot of one immutable revision.

    This is what a historical run keeps referencing (FR-80): the stored
    steps, ingredients, appliance requirements, variant links, and provenance
    byte-for-byte, with no re-derivation from the current recipe state.
    """
    revision = conn.execute(
        "SELECT id, recipe_id, revision_number, servings, total_time_minutes, "
        "steps_json, title, source_tier, protein_class, provenance "
        "FROM recipe_revisions WHERE id = ?",
        (revision_id,),
    ).fetchone()
    if revision is None:
        raise RecipeStorageError(f"unknown recipe revision {revision_id}")
    ingredient_rows = conn.execute(
        "SELECT ingredient_name, amount, unit, pantry_eligible, indivisible, "
        "package_unit, package_size, notes FROM recipe_ingredients "
        "WHERE revision_id = ? ORDER BY id",
        (revision_id,),
    ).fetchall()
    appliance_rows = conn.execute(
        "SELECT appliance_type, capability, model_constraint, capacity_constraint, "
        "capacity_unit FROM recipe_appliance_requirements "
        "WHERE revision_id = ? ORDER BY id",
        (revision_id,),
    ).fetchall()
    variant_rows = conn.execute(
        "SELECT l.recipe_id, v.variant_type FROM recipe_variants v "
        "JOIN recipes l ON l.id = v.linked_recipe_id "
        "WHERE v.revision_id = ? ORDER BY v.id",
        (revision_id,),
    ).fetchall()
    return {
        "revision_id": cast(int, revision[0]),
        "recipe_id": _revision_recipe_id(conn, cast(int, revision[0])),
        "revision_number": cast(int, revision[2]),
        "servings": cast(float, revision[3]),
        "total_time_minutes": revision[4],
        "steps": json.loads(cast(str, revision[5])),
        "title": revision[6],
        "source_tier": revision[7],
        "protein_class": revision[8],
        "provenance": revision[9],
        "ingredients": [
            {
                "name": cast(str, row[0]),
                "amount": cast(float, row[1]),
                "unit": cast(str, row[2]),
                "pantry_eligible": bool(row[3]),
                "indivisible": bool(row[4]),
                "package_unit": row[5],
                "package_size": row[6],
                "notes": row[7],
            }
            for row in ingredient_rows
        ],
        "appliances": [
            {
                "type": cast(str, row[0]),
                "capability": cast(str, row[1]),
                "model_constraint": row[2],
                "capacity_constraint": row[3],
                "capacity_unit": row[4],
            }
            for row in appliance_rows
        ],
        "variants": [
            {"linked_recipe_id": cast(str, row[0]), "variant_type": cast(str, row[1])}
            for row in variant_rows
        ],
    }


def _revision_recipe_id(conn: sqlite3.Connection, revision_id: int) -> str:
    row = conn.execute(
        "SELECT p.recipe_id FROM recipe_revisions r "
        "JOIN recipes p ON p.id = r.recipe_id WHERE r.id = ?",
        (revision_id,),
    ).fetchone()
    return cast(str, row[0]) if row is not None else ""


def insert_revision_for_existing_recipe(
    conn: sqlite3.Connection,
    *,
    recipe_id: str,
    title: str,
    reference_url: str | None,
    source_name: str,
    source_tier: str,
    cuisine: str | None,
    protein_class: str,
    total_time_minutes: int | None,
    servings: float,
    steps: list[str],
    ingredients: list[RecipeIngredient],
    appliances: list[ApplianceRequirement],
    variants: list[RecipeVariantLink],
    provenance: str,
    now: str,
) -> tuple[int, int]:
    """Create a new immutable revision for an existing recipe (FR-79, FR-80).

    Must run inside the caller's open transaction. The recipe must already
    exist (unlike :func:`insert_recipe_with_revision` which creates it).
    The new revision gets the next sequence number; the recipe's
    ``current_revision_id`` is updated to point to it. All prior revisions
    are retained unchanged (FR-79, FR-80).

    Returns ``(revision_row_id, revision_number)``.
    """
    recipe_row_id = _get_recipe_row_id(conn, recipe_id)
    if recipe_row_id is None:
        raise RecipeStorageError(f"recipe {recipe_id!r} does not exist; import it first")

    # Determine the next revision number
    row = conn.execute(
        "SELECT MAX(revision_number) FROM recipe_revisions WHERE recipe_id = ?",
        (recipe_row_id,),
    ).fetchone()
    max_num = cast(int, row[0]) if row and row[0] is not None else 0
    next_number = max_num + 1

    # Validate variant links reference known recipes
    for variant in variants:
        if _get_recipe_row_id(conn, variant.linked_recipe_id) is None:
            raise RecipeStorageError(
                f"variant link references unknown recipe "
                f"{variant.linked_recipe_id!r}; import the linked recipe first"
            )

    revision_row_id = cast(
        int,
        conn.execute(
            "INSERT INTO recipe_revisions (recipe_id, revision_number, source_name, "
            "reference_url, cuisine, servings, total_time_minutes, steps_json, "
            "provenance, accepted_at, created_at, title, source_tier, protein_class) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recipe_row_id,
                next_number,
                source_name,
                reference_url,
                cuisine,
                servings,
                total_time_minutes,
                json.dumps(steps, ensure_ascii=False),
                provenance,
                now,
                now,
                title,
                source_tier,
                protein_class,
            ),
        ).lastrowid,
    )

    # Update the recipe row to reflect the new current revision's identity fields
    conn.execute(
        "UPDATE recipes SET title = ?, reference_url = ?, source_name = ?, "
        "source_tier = ?, cuisine = ?, protein_class = ?, total_time_minutes = ?, "
        "current_revision_id = ?, updated_at = ? WHERE id = ?",
        (
            title,
            reference_url,
            source_name,
            source_tier,
            cuisine,
            protein_class,
            total_time_minutes,
            revision_row_id,
            now,
            recipe_row_id,
        ),
    )

    for ingredient in ingredients:
        conn.execute(
            "INSERT INTO recipe_ingredients (revision_id, ingredient_name, amount, "
            "unit, pantry_eligible, indivisible, package_unit, package_size, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_row_id,
                ingredient.name,
                ingredient.amount,
                ingredient.unit,
                1 if ingredient.pantry_eligible else 0,
                1 if ingredient.indivisible else 0,
                ingredient.package_unit,
                ingredient.package_size,
                ingredient.notes,
            ),
        )

    for appliance in appliances:
        conn.execute(
            "INSERT INTO recipe_appliance_requirements (revision_id, appliance_type, "
            "capability, model_constraint, capacity_constraint, capacity_unit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                revision_row_id,
                appliance.type,
                appliance.capability,
                appliance.model_constraint,
                appliance.capacity_constraint,
                appliance.capacity_unit,
            ),
        )

    for variant in variants:
        conn.execute(
            "INSERT INTO recipe_variants (revision_id, linked_recipe_id, variant_type, "
            "created_at) VALUES (?, ?, ?, ?)",
            (
                revision_row_id,
                _get_recipe_row_id(conn, variant.linked_recipe_id),
                variant.variant_type,
                now,
            ),
        )

    return revision_row_id, next_number
