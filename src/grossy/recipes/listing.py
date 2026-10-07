"""Read-only recipe library listing (no migration, lock or write)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from grossy.paths import GrossyPaths
from grossy.recipes.maintenance import RecipeMaintenanceError

LIST_SCHEMA_NAME = "grossy.recipes.list.v1"


def run_recipes_list(paths: GrossyPaths) -> dict[str, Any]:
    """List every stored recipe with its current revision summary."""
    if not paths.database_file.exists():
        raise RecipeMaintenanceError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )
    uri = paths.database_file.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.recipe_id, r.title, r.protein_class, r.cuisine, r.total_time_minutes, "
            "r.enabled, r.is_favorite, r.is_regular, r.base_weight, r.current_revision_id, "
            "v.revision_number, v.servings, v.source_name, "
            "(SELECT COUNT(*) FROM recipe_ingredients i WHERE i.revision_id=v.id) "
            "FROM recipes r LEFT JOIN recipe_revisions v ON v.id=r.current_revision_id "
            "ORDER BY r.recipe_id"
        ).fetchall()
    recipes = [
        {
            "recipe_id": r[0],
            "title": r[1],
            "protein_class": r[2],
            "cuisine": r[3],
            "total_time_minutes": r[4],
            "enabled": bool(r[5]),
            "favorite": bool(r[6]),
            "regular": bool(r[7]),
            "base_weight": r[8],
            "current_revision_id": r[9],
            "current_revision_number": r[10],
            "servings": r[11],
            "source_name": r[12],
            "ingredient_count": r[13] if r[9] is not None else 0,
        }
        for r in rows
    ]
    return {
        "schema_name": LIST_SCHEMA_NAME,
        "schema_version": 1,
        "status": "success",
        "recipe_count": len(recipes),
        "recipes": recipes,
    }
