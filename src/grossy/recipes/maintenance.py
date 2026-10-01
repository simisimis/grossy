"""Recipe maintenance commands (PRD FR-71, FR-104, 10.1; task 07).

Local user-maintenance commands for the offline recipe library:

- ``favorite``: favorite state, base weight 3 (FR-104);
- ``regular``: regular state, base weight 1 (FR-104);
- ``weight``: explicit editable base weight (FR-71);
- ``enable`` / ``disable``: enabled state (FR-71, FR-92).

Every command is explicit, transactional, and auditable: it records an
append-only event in ``recipe_events`` (one spelling per event type,
matching the schema CHECK constraint) with the caller's rationale or a
deterministic default. Favorite and regular are mutually exclusive
base-weight states: setting one clears the other. Maintenance never touches
revision data, so historical revision snapshots are preserved (FR-80).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.paths import GrossyPaths
from grossy.recipes.repository import get_recipe_row

# Event types match the `recipe_events.event_type` CHECK constraint.
EVENT_FAVORITE = "favorite"
EVENT_REGULAR = "regular"
EVENT_WEIGHT_CHANGED = "weight_changed"
EVENT_ENABLED = "enabled"
EVENT_DISABLED = "disabled"

FAVORITE_BASE_WEIGHT = 3.0
REGULAR_BASE_WEIGHT = 1.0


class RecipeMaintenanceError(Exception):
    """Raised when a recipe maintenance command fails validation or application."""


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_recipe_row_id(conn: sqlite3.Connection, recipe_id: str) -> int:
    row = conn.execute("SELECT id FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise RecipeMaintenanceError(f"unknown recipe {recipe_id!r}")
    return cast(int, row[0])


def _record_event(
    conn: sqlite3.Connection,
    recipe_row_id: int,
    event_type: str,
    detail: str | None,
    now: str,
) -> int:
    """Append one event to the recipe's history; return the event row id.

    ``recipe_events.recipe_id`` references ``recipes(id)`` (the integer row
    id), so the caller passes the resolved row id, not the string recipe id.
    """
    cur = conn.execute(
        "INSERT INTO recipe_events (recipe_id, event_type, run_id, detail, created_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        (recipe_row_id, event_type, detail, now),
    )
    return cast(int, cur.lastrowid)


def _run_recipe_maintenance(
    paths: GrossyPaths,
    operation: str,
    fn: Callable[[sqlite3.Connection, str], dict[str, object]],
) -> dict[str, object]:
    """Acquire the writer lock, run ``fn(conn, now)`` in a transaction, commit.

    ``fn`` receives the open connection and the current UTC instant. It must
    raise :class:`RecipeMaintenanceError` on validation failure (the
    transaction rolls back and state is unchanged). On success it returns a
    payload dict merged into the versioned JSON status emitted on stdout.
    """
    if not paths.database_file.exists():
        raise RecipeMaintenanceError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            now = _now_utc()
            payload = fn(conn, now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return {
        "schema_name": "grossy.recipes.maintenance",
        "schema_version": 1,
        "status": "success",
        "operation": operation,
        **payload,
    }


def run_recipe_favorite(
    paths: GrossyPaths, recipe_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Mark a recipe as favorite: favorite state, base weight 3 (FR-104)."""
    effective_rationale = rationale or f"manual correction: favorite recipe {recipe_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        row_id = _get_recipe_row_id(conn, recipe_id)
        row = cast(dict[str, Any], get_recipe_row(conn, recipe_id))
        previous_weight = cast(float, row["base_weight"])
        conn.execute(
            "UPDATE recipes SET is_favorite = 1, is_regular = 0, base_weight = ?, "
            "updated_at = ? WHERE id = ?",
            (FAVORITE_BASE_WEIGHT, now, row_id),
        )
        event_id = _record_event(
            conn,
            row_id,
            EVENT_FAVORITE,
            f"{previous_weight} -> {FAVORITE_BASE_WEIGHT}; {effective_rationale}",
            now,
        )
        return {
            "recipe_id": recipe_id,
            "is_favorite": True,
            "is_regular": False,
            "base_weight": FAVORITE_BASE_WEIGHT,
            "event_id": event_id,
        }

    return _run_recipe_maintenance(paths, f"favorite {recipe_id}", _do)


def run_recipe_regular(
    paths: GrossyPaths, recipe_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Mark a recipe as regular: regular state, base weight 1 (FR-104)."""
    effective_rationale = rationale or f"manual correction: regular recipe {recipe_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        row_id = _get_recipe_row_id(conn, recipe_id)
        row = cast(dict[str, Any], get_recipe_row(conn, recipe_id))
        previous_weight = cast(float, row["base_weight"])
        conn.execute(
            "UPDATE recipes SET is_regular = 1, is_favorite = 0, base_weight = ?, "
            "updated_at = ? WHERE id = ?",
            (REGULAR_BASE_WEIGHT, now, row_id),
        )
        event_id = _record_event(
            conn,
            row_id,
            EVENT_REGULAR,
            f"{previous_weight} -> {REGULAR_BASE_WEIGHT}; {effective_rationale}",
            now,
        )
        return {
            "recipe_id": recipe_id,
            "is_favorite": False,
            "is_regular": True,
            "base_weight": REGULAR_BASE_WEIGHT,
            "event_id": event_id,
        }

    return _run_recipe_maintenance(paths, f"regular {recipe_id}", _do)


def run_recipe_weight(
    paths: GrossyPaths, recipe_id: str, weight: float, rationale: str | None = None
) -> dict[str, object]:
    """Set an explicit base weight for a recipe (FR-71 editable base weight)."""
    if not math.isfinite(weight):
        raise RecipeMaintenanceError(f"weight must be a finite decimal, got {weight}")
    if weight <= 0:
        raise RecipeMaintenanceError(f"weight must be a positive decimal, got {weight}")
    effective_rationale = rationale or f"manual correction: weight {recipe_id} to {weight}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        row_id = _get_recipe_row_id(conn, recipe_id)
        row = cast(dict[str, Any], get_recipe_row(conn, recipe_id))
        previous_weight = cast(float, row["base_weight"])
        conn.execute(
            "UPDATE recipes SET base_weight = ?, updated_at = ? WHERE id = ?",
            (weight, now, row_id),
        )
        event_id = _record_event(
            conn,
            row_id,
            EVENT_WEIGHT_CHANGED,
            f"{previous_weight} -> {weight}; {effective_rationale}",
            now,
        )
        return {
            "recipe_id": recipe_id,
            "previous_base_weight": previous_weight,
            "base_weight": weight,
            "event_id": event_id,
        }

    return _run_recipe_maintenance(paths, f"weight {recipe_id} {weight}", _do)


def run_recipe_enable(
    paths: GrossyPaths, recipe_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Enable a disabled recipe (FR-71, FR-92)."""
    effective_rationale = rationale or f"manual correction: enable recipe {recipe_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        row_id = _get_recipe_row_id(conn, recipe_id)
        conn.execute(
            "UPDATE recipes SET enabled = 1, updated_at = ? WHERE id = ?",
            (now, row_id),
        )
        event_id = _record_event(conn, row_id, EVENT_ENABLED, effective_rationale, now)
        return {"recipe_id": recipe_id, "enabled": True, "event_id": event_id}

    return _run_recipe_maintenance(paths, f"enable {recipe_id}", _do)


def run_recipe_disable(
    paths: GrossyPaths, recipe_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Disable a recipe, removing it from automatic weekly eligibility (FR-92)."""
    effective_rationale = rationale or f"manual correction: disable recipe {recipe_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        row_id = _get_recipe_row_id(conn, recipe_id)
        conn.execute(
            "UPDATE recipes SET enabled = 0, updated_at = ? WHERE id = ?",
            (now, row_id),
        )
        event_id = _record_event(conn, row_id, EVENT_DISABLED, effective_rationale, now)
        return {"recipe_id": recipe_id, "enabled": False, "event_id": event_id}

    return _run_recipe_maintenance(paths, f"disable {recipe_id}", _do)
