"""Tests for CLI command discovery and fail-closed placeholder behavior.

Asserts that the complete deterministic command tree from PRD sections
10.1-10.2 is declared, and that placeholder commands emit versioned JSON on
stdout, diagnostics on stderr, and a non-zero exit code (PRD 10.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from grossy.cli import STATUS_SCHEMA_NAME, STATUS_SCHEMA_VERSION, app
from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths

RUNNER = CliRunner(mix_stderr=False)


# --- isolated temporary HOME/XDG fixtures ------------------------------------
#
# Every CLI invocation that reaches a real maintenance command resolves the
# operator's HOME/XDG paths through ``resolve_paths``. On a configured laptop
# those point at real state, so the tests below must run against a fresh, empty
# temporary home (never the real one) and assert the controlled missing database
# stays absent. See the task-06 test audit (safety finding).


def _paths_at(config_root: Path, data_root: Path, state_root: Path) -> GrossyPaths:
    return GrossyPaths(
        config_dir=config_root / "grossy",
        data_dir=data_root / "grossy",
        state_dir=state_root / "grossy",
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GrossyPaths:
    """A fresh, un-initialized home: no config, no database, no state.

    HOME and all three XDG base directories are pointed at the temp tree so a
    maintenance command that resolves paths never touches the real operator
    state. The database file is deliberately left absent.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    return _paths_at(home / ".config", home / ".local" / "share", home / ".local" / "state")


@pytest.fixture
def initialized_home(isolated_home: GrossyPaths) -> GrossyPaths:
    """A fresh home that has been initialized (config + migrated database)."""
    do_init(isolated_home)
    return isolated_home


EXPECTED_COMMANDS = frozenset(
    {
        "init",
        "doctor",
        "history set-primary",
        "history set-recurrence",
        "history set-quantity",
        "history disable",
        "history enable",
        "history blacklist product",
        "history blacklist family",
        "history unblacklist product",
        "history unblacklist family",
        "history family link",
        "history family unlink",
        "history family merge",
        "history set-category",
        "history category rename",
        "history discover-prepare",
        "history sync-dry-run",
        "history scan-prepare",
        "history scan-validate",
        "history sync-apply",
        "recipes add-file",
        "recipes favorite",
        "recipes regular",
        "recipes weight",
        "recipes enable",
        "recipes disable",
        "recipes import-validate",
        "recipes import-apply",
        "recipes refresh-diff",
        "recipes refresh-apply",
        "weekly finish",
        "weekly abandon",
        "weekly plan-prepare",
        "weekly baseline-apply",
        "weekly recurring-preflight",
        "weekly recurring-intent",
        "weekly recurring-observe",
        "weekly recipes-apply",
        "weekly reroll-apply",
        "weekly selection-apply",
        "weekly recipe-preflight",
        "weekly recipe-intent",
        "weekly recipe-observe",
        "weekly removal-preflight",
        "weekly removal-intent",
        "weekly removal-observe",
        "weekly resume-prepare",
        "db backup",
        "db export",
        "db restore",
    }
)


def collect_commands(command: click.Command, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if not isinstance(command, click.Group):
        return paths
    for name, sub in command.commands.items():
        path = f"{prefix} {name}".strip()
        if isinstance(sub, click.Group):
            paths |= collect_commands(sub, path)
        else:
            paths.add(path)
    return paths


def test_full_command_tree_is_declared() -> None:
    root = get_command(app)
    assert collect_commands(root) == EXPECTED_COMMANDS


def test_no_purge_or_reset_anywhere() -> None:
    root = get_command(app)
    collected = collect_commands(root)
    assert not any(part in ("purge", "reset") for path in collected for part in path.split())


def test_help_exits_zero() -> None:
    result = RUNNER.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("history", "recipes", "weekly", "db"):
        assert group in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["recipes", "favorite", "r-1"],
        ["recipes", "regular", "r-1"],
        ["recipes", "weight", "r-1", "1.5"],
        ["recipes", "enable", "r-1"],
        ["recipes", "disable", "r-1"],
        ["weekly", "finish", "run-1"],
        ["weekly", "finish", "run-1", "--no-recipes"],
        ["weekly", "abandon", "run-1"],
        ["weekly", "plan-prepare"],
        ["weekly", "plan-prepare", "--skip-history-sync"],
        ["weekly", "reroll-apply", "run-1", "recipe1", "--from", "meat"],
        ["weekly", "selection-apply", "run-1", "recipe1,recipe2"],
        ["weekly", "removal-preflight", "run-1", "--product", "1234567"],
    ],
)
def test_placeholder_fails_closed_with_versioned_json(args: list[str]) -> None:
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 1
    stdout: dict[str, Any] = json.loads(result.output)
    assert stdout["schema_name"] == STATUS_SCHEMA_NAME
    assert stdout["schema_version"] == STATUS_SCHEMA_VERSION
    assert stdout["status"] == "not_implemented"
    assert isinstance(stdout["command"], str) and stdout["command"].startswith(args[0])
    # Diagnostics go to stderr, not stdout: stdout is exactly one JSON line.
    assert result.output.count("\n") <= 1


@pytest.mark.parametrize(
    "args",
    [
        ["history", "set-primary", "fam-1", "1234567"],
        ["history", "set-recurrence", "fam-1", "--weeks", "2"],
        ["history", "set-quantity", "fam-1", "2"],
        ["history", "disable", "fam-1"],
        ["history", "enable", "fam-1"],
        ["history", "blacklist", "product", "1234567"],
        ["history", "blacklist", "family", "fam-1"],
        ["history", "unblacklist", "product", "1234567"],
        ["history", "unblacklist", "family", "fam-1"],
        ["history", "family", "link", "1234567", "fam-1"],
        ["history", "family", "unlink", "1234567"],
        ["history", "family", "merge", "fam-1", "fam-2"],
        ["history", "set-category", "fam-1", "breakfast"],
        ["history", "category", "rename", "old", "new"],
    ],
)
def test_maintenance_commands_fail_closed_without_database(
    args: list[str], isolated_home: GrossyPaths
) -> None:
    """Maintenance commands fail with a versioned JSON error when no DB exists.

    Runs against a fresh, un-initialized temporary home so the real operator
    state is never touched. The controlled missing database must remain absent:
    the runner refuses to create or migrate it.
    """
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 1
    stdout: dict[str, Any] = json.loads(result.output)
    assert stdout["schema_name"] == STATUS_SCHEMA_NAME
    assert stdout["schema_version"] == STATUS_SCHEMA_VERSION
    assert stdout["status"] == "error"
    # Assert the correct complete command label, not just a prefix.
    assert stdout["command"] == " ".join(args)
    assert "database not found" in stdout["detail"]
    # The controlled missing database must not have been created or migrated.
    assert not isolated_home.database_file.exists()
    # And the writer lock must not have been acquired for a missing database.
    assert not (isolated_home.state_dir / "writer.lock").exists()


def test_nonnumeric_weeks_rejected_by_parser(isolated_home: GrossyPaths) -> None:
    """Click's int type rejects non-numeric --weeks before the handler executes."""
    with patch("grossy.cli.run_set_recurrence") as handler:
        result = RUNNER.invoke(app, ["history", "set-recurrence", "fam-1", "--weeks", "zero"])
    handler.assert_not_called()
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--weeks" in result.stderr
    assert "not a valid integer" in result.stderr


@pytest.mark.parametrize(
    ("args", "diagnostic", "spy_target"),
    [
        pytest.param(
            ["history", "set-quantity", "fam-1", "0"],
            "quantity must be a positive integer",
            "grossy.cli.run_set_quantity",
            id="zero-history-quantity",
        ),
        pytest.param(
            ["recipes", "weight", "r-1", "0"],
            "weight must be a positive decimal",
            "grossy.cli.emit_not_implemented",
            id="zero-recipe-weight",
        ),
        pytest.param(
            ["weekly", "removal-preflight", "run-1", "--product", "1", "--quantity", "-2"],
            "--quantity must be a positive integer",
            "grossy.cli.emit_not_implemented",
            id="negative-removal-quantity",
        ),
    ],
)
def test_nonpositive_arguments_rejected_before_handler(
    args: list[str], diagnostic: str, spy_target: str, isolated_home: GrossyPaths
) -> None:
    """Range validation (require_positive) rejects non-positive values before
    the maintenance handler or placeholder is reached."""
    with patch(spy_target) as spy:
        result = RUNNER.invoke(app, args)
    spy.assert_not_called()
    assert result.exit_code == 2
    assert result.stdout == ""
    assert diagnostic in result.stderr


# --- successful / error CLI behavior against temporary initialized state ------
#
# These go through the full CLI path (``resolve_paths`` -> ``run_*`` -> versioned
# JSON) against a temporary initialized home, not the service functions directly.


def _seed_product(paths: GrossyPaths, product_id: str, name: str = "Olympus Milk 1L") -> None:
    conn = open_connection(paths.database_file)
    try:
        now = "2026-09-26T12:00:00.000000Z"
        conn.execute(
            "INSERT INTO products (retailer, retailer_product_id, canonical_url, name, "
            "purchase_order_count, created_at, updated_at) "
            "VALUES ('ab', ?, ?, ?, 1, ?, ?)",
            (
                product_id,
                f"https://www.ab.gr/en/eshop/x/{product_id}/p/{product_id}",
                name,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_product_category(paths: GrossyPaths, product_id: str) -> str | None:
    conn = open_connection(paths.database_file)
    try:
        row = conn.execute(
            "SELECT c.name FROM products p "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "WHERE p.retailer = 'ab' AND p.retailer_product_id = ?",
            (product_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_cli_set_category_success(initialized_home: GrossyPaths) -> None:
    _seed_product(initialized_home, "7000001")
    result = RUNNER.invoke(app, ["history", "set-category", "7000001", "Dairy Products"])
    assert result.exit_code == 0
    stdout: dict[str, Any] = json.loads(result.output)
    # A successful maintenance command emits its own versioned result schema.
    assert stdout["schema_name"] == "grossy.history.maintenance"
    assert stdout["schema_version"] == 1
    assert stdout["status"] == "success"
    assert stdout["operation"] == "set-category 7000001 Dairy Products"
    # The category assignment actually persisted.
    assert _read_product_category(initialized_home, "7000001") == "dairy products"


def test_cli_set_category_misc_rejected(initialized_home: GrossyPaths) -> None:
    _seed_product(initialized_home, "7000002")
    result = RUNNER.invoke(app, ["history", "set-category", "7000002", "misc"])
    assert result.exit_code == 1
    stdout: dict[str, Any] = json.loads(result.output)
    assert stdout["status"] == "error"
    assert "misc" in stdout["detail"]
    # The product keeps its existing category (unassigned) and no misc row is created.
    assert _read_product_category(initialized_home, "7000002") is None
    conn = open_connection(initialized_home.database_file)
    try:
        row = conn.execute("SELECT 1 FROM categories WHERE name = 'misc'").fetchone()
        assert row is None, "no-misc rule violated: a 'misc' category row was created"
    finally:
        conn.close()


def test_cli_unknown_target_reports_error(initialized_home: GrossyPaths) -> None:
    result = RUNNER.invoke(app, ["history", "blacklist", "product", "9999999"])
    assert result.exit_code == 1
    stdout: dict[str, Any] = json.loads(result.output)
    assert stdout["schema_name"] == STATUS_SCHEMA_NAME
    assert stdout["schema_version"] == STATUS_SCHEMA_VERSION
    assert stdout["status"] == "error"
    assert "unknown product" in stdout["detail"]
    # Diagnostics go to stderr, not stdout: stdout is exactly one JSON line.
    assert result.output.count("\n") <= 1


# --- writer-lock regression (R4) ---------------------------------------------


def test_maintenance_command_respects_writer_lock(initialized_home: GrossyPaths) -> None:
    """A maintenance command that would otherwise succeed fails with a versioned
    lock error when the writer lock is held; it succeeds after release.

    This proves the maintenance CLI wiring actually acquires the WriterLock
    (not just that the lock primitive exists in isolation).
    """
    from grossy.db.lock import WriterLock

    _seed_product(initialized_home, "7000003")

    with WriterLock(initialized_home.state_dir):
        result = RUNNER.invoke(app, ["history", "set-quantity", "7000003", "3"])
        assert result.exit_code == 1
        stdout: dict[str, Any] = json.loads(result.output)
        assert stdout["schema_name"] == STATUS_SCHEMA_NAME
        assert stdout["schema_version"] == STATUS_SCHEMA_VERSION
        assert stdout["status"] == "error"
        assert stdout["command"] == "history set-quantity 7000003 3"
        assert "writer lock held" in stdout["detail"]

        # Domain state is unchanged: quantity still at default.
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT typical_quantity FROM products WHERE retailer_product_id = '7000003'"
            ).fetchone()
            assert row is not None
            assert row[0] == 1  # unchanged from default
            assert conn.execute("SELECT * FROM manual_overrides").fetchall() == []
            assert conn.execute("SELECT * FROM blacklist_events").fetchall() == []
        finally:
            conn.close()

    # After releasing the lock, the same command succeeds.
    result = RUNNER.invoke(app, ["history", "set-quantity", "7000003", "3"])
    assert result.exit_code == 0
    stdout = json.loads(result.output)
    assert stdout["status"] == "success"
    conn = open_connection(initialized_home.database_file)
    try:
        row = conn.execute(
            "SELECT typical_quantity FROM products WHERE retailer_product_id = '7000003'"
        ).fetchone()
        assert row is not None
        assert row[0] == 3
    finally:
        conn.close()
