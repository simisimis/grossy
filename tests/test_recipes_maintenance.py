"""Tests for recipe maintenance commands (PRD FR-71, FR-104, FR-92; task 07).

favorite / regular / weight / enable / disable are explicit, transactional,
and auditable: each records an append-only ``recipe_events`` row and never
touches revision data, so historical revision snapshots are preserved
(FR-80). Favorite and regular are mutually exclusive base-weight states.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.init import do_init
from grossy.paths import GrossyPaths
from grossy.recipes import (
    RecipeIngredient,
    RecipeMaintenanceError,
    get_revision_snapshot,
    insert_recipe_with_revision,
    run_recipe_disable,
    run_recipe_enable,
    run_recipe_favorite,
    run_recipe_regular,
    run_recipe_weight,
)

NOW = "2026-09-26T12:00:00.000000Z"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    return tmp_path, paths


def _db(paths: GrossyPaths) -> sqlite3.Connection:
    return open_connection(paths.database_file)


def _count_recipe(conn: sqlite3.Connection, recipe_id: str) -> int:
    """Count recipe rows for a specific recipe_id (isolates from seeded rows)."""
    return conn.execute(
        "SELECT COUNT(*) FROM recipes WHERE recipe_id = ?", (recipe_id,)
    ).fetchone()[0]


def _seed(
    paths: GrossyPaths,
    recipe_id: str,
    *,
    base_weight: float = 1.0,
    is_favorite: bool = False,
    is_regular: bool = True,
    enabled: bool = True,
) -> int:
    """Insert one recipe via the real write path; return its revision id."""
    with WriterLock(paths.state_dir):
        conn = open_connection(paths.database_file)
        try:
            _, revision_id = insert_recipe_with_revision(
                conn,
                recipe_id=recipe_id,
                title=f"Title for {recipe_id}",
                reference_url=None,
                source_name="user",
                source_tier="user",
                cuisine=None,
                protein_class="meat",
                total_time_minutes=None,
                servings=2.0,
                steps=["Do the thing"],
                ingredients=[RecipeIngredient(name="flour", amount=100.0, unit="g")],
                appliances=[],
                variants=[],
                base_weight=base_weight,
                is_favorite=is_favorite,
                is_regular=is_regular,
                enabled=enabled,
                provenance="user-file",
                now=NOW,
            )
            conn.commit()
            return revision_id
        finally:
            conn.close()


def _state(paths: GrossyPaths, recipe_id: str) -> tuple[float, int, int, int]:
    conn = _db(paths)
    try:
        row = conn.execute(
            "SELECT base_weight, is_favorite, is_regular, enabled FROM recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        assert row is not None
        return float(row[0]), int(row[1]), int(row[2]), int(row[3])
    finally:
        conn.close()


def _events(paths: GrossyPaths, recipe_id: str) -> list[tuple[str, str | None]]:
    conn = _db(paths)
    try:
        rows = conn.execute(
            "SELECT e.event_type, e.detail FROM recipe_events e "
            "JOIN recipes r ON r.id = e.recipe_id WHERE r.recipe_id = ? "
            "ORDER BY e.id",
            (recipe_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


# --- state transitions -----------------------------------------------------------


def test_favorite_sets_state_and_event(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1")
    result = run_recipe_favorite(paths, "r-1")
    assert result["status"] == "success"
    assert result["is_favorite"] is True
    assert result["is_regular"] is False
    assert result["base_weight"] == 3.0
    assert _state(paths, "r-1") == (3.0, 1, 0, 1)
    events = _events(paths, "r-1")
    assert len(events) == 1
    assert events[0][0] == "favorite"
    assert "1.0 -> 3.0" in events[0][1]


def test_regular_clears_favorite(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1", is_favorite=True, is_regular=False, base_weight=3.0)
    run_recipe_regular(paths, "r-1")
    assert _state(paths, "r-1") == (1.0, 0, 1, 1)
    assert [e[0] for e in _events(paths, "r-1")] == ["regular"]


def test_weight_sets_explicit_value(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1")
    result = run_recipe_weight(paths, "r-1", 2.5)
    assert result["previous_base_weight"] == 1.0
    assert result["base_weight"] == 2.5
    # Flags are untouched by an explicit weight correction.
    assert _state(paths, "r-1") == (2.5, 0, 1, 1)
    events = _events(paths, "r-1")
    assert events[0][0] == "weight_changed"
    assert "1.0 -> 2.5" in events[0][1]


def test_enable_disable_toggle(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1", enabled=False)
    run_recipe_enable(paths, "r-1")
    assert _state(paths, "r-1") == (1.0, 0, 1, 1)
    run_recipe_disable(paths, "r-1")
    assert _state(paths, "r-1") == (1.0, 0, 1, 0)
    assert [e[0] for e in _events(paths, "r-1")] == ["enabled", "disabled"]


def test_rationale_recorded_in_event_detail(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1")
    run_recipe_weight(paths, "r-1", 2.0, rationale="user said this is a light dish")
    events = _events(paths, "r-1")
    assert "user said this is a light dish" in events[0][1]


# --- fail-closed behavior ---------------------------------------------------------


def test_unknown_recipe_fails_without_event(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    with pytest.raises(RecipeMaintenanceError, match="unknown recipe"):
        run_recipe_favorite(paths, "ghost-recipe")
    conn = _db(paths)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recipe_events").fetchone()[0] == 0
        assert _count_recipe(conn, "ghost-recipe") == 0
    finally:
        conn.close()


def test_nonpositive_weight_fails_state_unchanged(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1")
    with pytest.raises(RecipeMaintenanceError, match="positive decimal"):
        run_recipe_weight(paths, "r-1", 0.0)
    assert _state(paths, "r-1") == (1.0, 0, 1, 1)
    assert _events(paths, "r-1") == []


def test_nonfinite_weight_fails_state_unchanged(env: tuple[Path, GrossyPaths]) -> None:
    """inf and nan are rejected without changing state or recording an event."""
    _tmp, paths = env
    _seed(paths, "r-1")
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(RecipeMaintenanceError, match="finite decimal"):
            run_recipe_weight(paths, "r-1", bad)
    assert _state(paths, "r-1") == (1.0, 0, 1, 1)
    assert _events(paths, "r-1") == []


def test_missing_database_fails_closed(tmp_path: Path) -> None:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    with pytest.raises(RecipeMaintenanceError, match="database not found"):
        run_recipe_enable(paths, "r-1")
    assert not paths.database_file.exists()
    assert not (paths.state_dir / "writer.lock").exists()


def test_writer_lock_respected(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1")
    with WriterLock(paths.state_dir), pytest.raises(Exception) as excinfo:
        run_recipe_favorite(paths, "r-1")
    assert "lock" in str(excinfo.value).lower()
    assert _state(paths, "r-1") == (1.0, 0, 1, 1)
    assert _events(paths, "r-1") == []


# --- audit trail and immutability --------------------------------------------------


def test_events_are_append_only_history(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    _seed(paths, "r-1")
    run_recipe_favorite(paths, "r-1")
    run_recipe_weight(paths, "r-1", 2.0)
    run_recipe_disable(paths, "r-1")
    run_recipe_enable(paths, "r-1")
    run_recipe_regular(paths, "r-1")
    assert [e[0] for e in _events(paths, "r-1")] == [
        "favorite",
        "weight_changed",
        "disabled",
        "enabled",
        "regular",
    ]


def test_maintenance_does_not_touch_revisions(env: tuple[Path, GrossyPaths]) -> None:
    _tmp, paths = env
    revision_id = _seed(paths, "r-1")
    snapshot_before = get_revision_snapshot(_db(paths), revision_id)

    run_recipe_favorite(paths, "r-1")
    run_recipe_weight(paths, "r-1", 4.0)
    run_recipe_disable(paths, "r-1")

    snapshot_after = get_revision_snapshot(_db(paths), revision_id)
    assert snapshot_after == snapshot_before
    conn = _db(paths)
    try:
        # Exactly one revision before and after: maintenance never revisions.
        # Filtered to r-1 so the seeded recipe's revision is not counted.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM recipe_revisions r "
                "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = 'r-1'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
