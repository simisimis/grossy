"""Grossy deterministic CLI shell (PRD sections 10.1-10.2, 10.3).

Every public and deterministic command is declared with its exact argument
and option surface. Commands whose behavior belongs to a later task are
explicit fail-closed placeholders: they emit a versioned JSON status on
stdout, write diagnostics to stderr, and exit non-zero (PRD 10.3.1-3).
No command invokes a live browser implicitly (PRD 10.3.9), and no
namespace provides purge or reset (PRD 10.3.10).
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from grossy.db import LockHeldError
from grossy.db import db_backup as _db_backup
from grossy.db import db_export as _db_export
from grossy.db import db_restore as _db_restore
from grossy.history import (
    HistorySyncError,
    run_discover_prepare,
    run_scan_prepare,
    run_scan_validate,
    run_sync_apply,
    run_sync_dry_run,
)
from grossy.history.maintenance import (
    MaintenanceError,
    run_blacklist_family,
    run_blacklist_product,
    run_category_rename,
    run_disable,
    run_enable,
    run_family_link,
    run_family_merge,
    run_family_unlink,
    run_set_category,
    run_set_primary,
    run_set_quantity,
    run_set_recurrence,
    run_unblacklist_family,
    run_unblacklist_product,
)
from grossy.init import InitError, do_init
from grossy.paths import resolve_paths
from grossy.recipes import (
    DiscoveryError,
    RecipeImportError,
    RecipeMaintenanceError,
    RecipeRefreshError,
    RecipeYamlError,
    SeedImportError,
    run_add_file,
    run_import_apply,
    run_import_validate,
    run_recipe_disable,
    run_recipe_enable,
    run_recipe_favorite,
    run_recipe_regular,
    run_recipe_weight,
    run_recipes_discover,
    run_refresh_apply,
    run_seed_import,
)

STATUS_SCHEMA_NAME = "grossy.cli.status"
STATUS_SCHEMA_VERSION = 1


def emit_not_implemented(command: str) -> NoReturn:
    """Fail closed for a declared-but-not-yet-implemented command."""
    payload = {
        "schema_name": STATUS_SCHEMA_NAME,
        "schema_version": STATUS_SCHEMA_VERSION,
        "status": "not_implemented",
        "command": command,
        "detail": "this command is declared but its behavior is implemented by a later task; "
        "no state or browser mutation was attempted",
    }
    typer.echo(json.dumps(payload, sort_keys=True))
    typer.echo(f"grossy: '{command}' is not implemented yet; refusing to continue", err=True)
    raise typer.Exit(code=1)


def emit_history_error(command: str, exc: Exception) -> NoReturn:
    """Fail closed for a history synchronization phase error."""
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "error",
                "command": command,
                "detail": str(exc),
            },
            sort_keys=True,
        )
    )
    typer.echo(f"grossy: {exc}", err=True)
    raise typer.Exit(code=1)


def require_positive(value: int, name: str) -> None:
    if value <= 0:
        typer.echo(f"grossy: {name} must be a positive integer, got {value}", err=True)
        raise typer.Exit(code=2)


def emit_maintenance_error(command: str, exc: Exception) -> NoReturn:
    """Fail closed for a manual correction command error."""
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "error",
                "command": command,
                "detail": str(exc),
            },
            sort_keys=True,
        )
    )
    typer.echo(f"grossy: {exc}", err=True)
    raise typer.Exit(code=1)


def require_positive_decimal(value: float, name: str) -> None:
    if not math.isfinite(value):
        typer.echo(f"grossy: {name} must be a finite decimal, got {value}", err=True)
        raise typer.Exit(code=2)
    if value <= 0:
        typer.echo(f"grossy: {name} must be a positive decimal, got {value}", err=True)
        raise typer.Exit(code=2)


def emit_recipe_error(command: str, exc: Exception) -> NoReturn:
    """Fail closed for a recipe import, maintenance, or storage error."""
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "error",
                "command": command,
                "detail": str(exc),
            },
            sort_keys=True,
        )
    )
    typer.echo(f"grossy: {exc}", err=True)
    raise typer.Exit(code=1)


def emit_weekly_error(command: str, exc: Exception) -> NoReturn:
    """Fail closed for a weekly planning phase error."""
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "error",
                "command": command,
                "detail": str(exc),
            },
            sort_keys=True,
        )
    )
    typer.echo(f"grossy: {exc}", err=True)
    raise typer.Exit(code=1)


app = typer.Typer(
    help="Grossy: deterministic AB.gr weekly grocery planning CLI", no_args_is_help=True
)
history_app = typer.Typer(help="Learned history and manual corrections", no_args_is_help=True)
recipes_app = typer.Typer(help="Offline recipe library", no_args_is_help=True)
weekly_app = typer.Typer(help="Weekly run lifecycle phases", no_args_is_help=True)
db_app = typer.Typer(help="Database backup, export, and restore", no_args_is_help=True)

blacklist_app = typer.Typer(help="Block products or families", no_args_is_help=True)
unblacklist_app = typer.Typer(help="Unblock products or families", no_args_is_help=True)
family_app = typer.Typer(help="Product family operations", no_args_is_help=True)
category_app = typer.Typer(help="Category operations", no_args_is_help=True)

app.add_typer(history_app, name="history")
app.add_typer(recipes_app, name="recipes")
app.add_typer(weekly_app, name="weekly")
app.add_typer(db_app, name="db")
history_app.add_typer(blacklist_app, name="blacklist")
history_app.add_typer(unblacklist_app, name="unblacklist")
history_app.add_typer(family_app, name="family")
history_app.add_typer(category_app, name="category")


# --- PRD 10.1: user-maintenance commands ---------------------------------


@app.command()
def init() -> None:
    """Create XDG directories, sample configuration, database, and permissions."""
    try:
        paths = resolve_paths()
        result = do_init(paths)
    except InitError as exc:
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "init",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        typer.echo(f"grossy init: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, sort_keys=True))


@app.command()
def doctor() -> None:
    """Validate the local environment and authenticated read-only AB.gr access."""
    from grossy.doctor import run_doctor

    paths = resolve_paths()
    result = run_doctor(paths)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))
    if result.has_failures:
        raise typer.Exit(code=1)
    if result.has_warnings:
        raise typer.Exit(code=2)


@history_app.command("set-primary")
def history_set_primary(
    family_id: str,
    product_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Explicitly set the primary product of an established family."""
    try:
        paths = resolve_paths()
        result = run_set_primary(paths, family_id, product_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history set-primary {family_id} {product_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@history_app.command("set-recurrence")
def history_set_recurrence(
    family_id: str,
    weeks: Annotated[int, typer.Option("--weeks")],
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Explicitly set a family recurrence interval in whole weeks."""
    require_positive(weeks, "--weeks")
    try:
        paths = resolve_paths()
        result = run_set_recurrence(paths, family_id, weeks, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history set-recurrence {family_id} --weeks {weeks}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@history_app.command("set-quantity")
def history_set_quantity(
    product_or_family_id: str,
    quantity: int,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Explicitly set a typical quantity for a product or family."""
    require_positive(quantity, "quantity")
    try:
        paths = resolve_paths()
        result = run_set_quantity(paths, product_or_family_id, quantity, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history set-quantity {product_or_family_id} {quantity}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@history_app.command("disable")
def history_disable(
    family_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Disable a family, suppressing history-driven recurring prefill."""
    try:
        paths = resolve_paths()
        result = run_disable(paths, family_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history disable {family_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@history_app.command("enable")
def history_enable(
    family_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Re-enable a disabled family."""
    try:
        paths = resolve_paths()
        result = run_enable(paths, family_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history enable {family_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@blacklist_app.command("product")
def history_blacklist_product(
    product_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Block an exact product from all automatic selection."""
    try:
        paths = resolve_paths()
        result = run_blacklist_product(paths, product_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history blacklist product {product_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@blacklist_app.command("family")
def history_blacklist_family(
    family_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Block every member of a family everywhere."""
    try:
        paths = resolve_paths()
        result = run_blacklist_family(paths, family_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history blacklist family {family_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@unblacklist_app.command("product")
def history_unblacklist_product(
    product_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Remove an explicit product blacklist."""
    try:
        paths = resolve_paths()
        result = run_unblacklist_product(paths, product_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history unblacklist product {product_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@unblacklist_app.command("family")
def history_unblacklist_family(
    family_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Remove an explicit family blacklist."""
    try:
        paths = resolve_paths()
        result = run_unblacklist_family(paths, family_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history unblacklist family {family_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@family_app.command("link")
def history_family_link(
    product_id: str,
    family_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Link an exact product into a family."""
    try:
        paths = resolve_paths()
        result = run_family_link(paths, product_id, family_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history family link {product_id} {family_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@family_app.command("unlink")
def history_family_unlink(
    product_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Remove a product from its family."""
    try:
        paths = resolve_paths()
        result = run_family_unlink(paths, product_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history family unlink {product_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@family_app.command("merge")
def history_family_merge(
    source_family_id: str,
    target_family_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Merge one family into another."""
    try:
        paths = resolve_paths()
        result = run_family_merge(paths, source_family_id, target_family_id, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history family merge {source_family_id} {target_family_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@history_app.command("set-category")
def history_set_category(
    product_or_family_id: str,
    category: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Explicitly assign a normalized category."""
    try:
        paths = resolve_paths()
        result = run_set_category(paths, product_or_family_id, category, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history set-category {product_or_family_id} {category}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@category_app.command("rename")
def history_category_rename(
    old_name: str,
    new_name: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Rename a normalized category transactionally."""
    try:
        paths = resolve_paths()
        result = run_category_rename(paths, old_name, new_name, rationale)
    except (MaintenanceError, LockHeldError) as exc:
        emit_maintenance_error(f"history category rename {old_name} {new_name}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@recipes_app.command("add-file")
def recipes_add_file(
    recipe_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Import a user-authored YAML recipe (no public URL required)."""
    try:
        paths = resolve_paths()
        result = run_add_file(paths, recipe_file)
    except (RecipeYamlError, RecipeImportError, RecipeMaintenanceError, LockHeldError) as exc:
        emit_recipe_error(f"recipes add-file {recipe_file}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("favorite")
def recipes_favorite(
    recipe_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Mark a recipe as favorite (base weight 3)."""
    try:
        paths = resolve_paths()
        result = run_recipe_favorite(paths, recipe_id, rationale)
    except (RecipeMaintenanceError, LockHeldError) as exc:
        emit_recipe_error(f"recipes favorite {recipe_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@recipes_app.command("regular")
def recipes_regular(
    recipe_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Mark a recipe as regular (base weight 1)."""
    try:
        paths = resolve_paths()
        result = run_recipe_regular(paths, recipe_id, rationale)
    except (RecipeMaintenanceError, LockHeldError) as exc:
        emit_recipe_error(f"recipes regular {recipe_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@recipes_app.command("weight")
def recipes_weight(
    recipe_id: str,
    weight: float,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Set an explicit base weight for a recipe."""
    require_positive_decimal(weight, "weight")
    try:
        paths = resolve_paths()
        result = run_recipe_weight(paths, recipe_id, weight, rationale)
    except (RecipeMaintenanceError, LockHeldError) as exc:
        emit_recipe_error(f"recipes weight {recipe_id} {weight}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@recipes_app.command("enable")
def recipes_enable(
    recipe_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Enable a recipe."""
    try:
        paths = resolve_paths()
        result = run_recipe_enable(paths, recipe_id, rationale)
    except (RecipeMaintenanceError, LockHeldError) as exc:
        emit_recipe_error(f"recipes enable {recipe_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@recipes_app.command("disable")
def recipes_disable(
    recipe_id: str,
    rationale: Annotated[str | None, typer.Option("--rationale")] = None,
) -> None:
    """Disable a recipe."""
    try:
        paths = resolve_paths()
        result = run_recipe_disable(paths, recipe_id, rationale)
    except (RecipeMaintenanceError, LockHeldError) as exc:
        emit_recipe_error(f"recipes disable {recipe_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@weekly_app.command("finish")
def weekly_finish(
    run_id: str,
    no_recipes: Annotated[bool, typer.Option("--no-recipes")] = False,
) -> None:
    """Record that the user reviewed the prepared cart."""
    emit_not_implemented(f"weekly finish {run_id} --no-recipes={no_recipes}")


@weekly_app.command("abandon")
def weekly_abandon(run_id: str) -> None:
    """Close a run without removing any cart additions."""
    emit_not_implemented(f"weekly abandon {run_id}")


@db_app.command("backup")
def db_backup(path: Path) -> None:
    """Create a SQLite-safe backup with a schema-version manifest and checksum."""
    paths = resolve_paths()
    if not paths.database_file.exists():
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "db backup",
                    "detail": f"database not found at {paths.database_file}; run init first",
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1)
    try:
        result = _db_backup(paths.database_file, path)
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "db backup",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1) from exc
    manifest_path = result.with_suffix(result.suffix + ".manifest.json")
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "success",
                "command": "db backup",
                "backup_path": str(result),
                "manifest_path": str(manifest_path),
            },
            sort_keys=True,
        )
    )


@db_app.command("export")
def db_export(path: Path) -> None:
    """Write a versioned JSON export."""
    paths = resolve_paths()
    if not paths.database_file.exists():
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "db export",
                    "detail": f"database not found at {paths.database_file}; run init first",
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1)
    try:
        result = _db_export(paths.database_file, path)
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "db export",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "success",
                "command": "db export",
                "export_path": str(result),
            },
            sort_keys=True,
        )
    )


@db_app.command("restore")
def db_restore(
    backup: Path,
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
) -> None:
    """Restore a verified backup under the PRD restore constraints."""
    paths = resolve_paths()
    if not paths.database_file.exists():
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "db restore",
                    "detail": f"database not found at {paths.database_file}; run init first",
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1)
    try:
        info = _db_restore(paths.database_file, backup, paths.state_dir, confirm=confirm)
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "error",
                    "command": "db restore",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1) from exc
    if not confirm:
        # Validation passed but confirmation not provided.
        typer.echo(
            json.dumps(
                {
                    "schema_name": STATUS_SCHEMA_NAME,
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "status": "confirmation_required",
                    "command": "db restore",
                    "source_schema_version": info.source_schema_version,
                    "target_schema_version": info.target_schema_version,
                    "detail": f"validation passed; re-run with --confirm to restore from {backup}",
                },
                sort_keys=True,
            )
        )
        raise typer.Exit(code=1)
    typer.echo(
        json.dumps(
            {
                "schema_name": STATUS_SCHEMA_NAME,
                "schema_version": STATUS_SCHEMA_VERSION,
                "status": "success",
                "command": "db restore",
                "source_schema_version": info.source_schema_version,
                "target_schema_version": info.target_schema_version,
            },
            sort_keys=True,
        )
    )


# --- PRD 10.2: deterministic workflow commands ----------------------------


@history_app.command("discover-prepare")
def history_discover_prepare() -> None:
    """Prepare the observable-six listed-order discovery phase."""
    try:
        paths = resolve_paths()
        result = run_discover_prepare(paths)
    except HistorySyncError as exc:
        emit_history_error("history discover-prepare", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@history_app.command("sync-dry-run")
def history_sync_dry_run(
    listed_orders: Annotated[Path, typer.Option("--listed-orders", exists=True, dir_okay=False)],
) -> None:
    """Report only the count of unknown listed source order IDs."""
    try:
        paths = resolve_paths()
        result = run_sync_dry_run(listed_orders, paths)
    except HistorySyncError as exc:
        emit_history_error(f"history sync-dry-run --listed-orders {listed_orders}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@history_app.command("scan-prepare")
def history_scan_prepare(
    listed_orders: Annotated[Path, typer.Option("--listed-orders", exists=True, dir_okay=False)],
) -> None:
    """Expose the exact unknown orders to scan, oldest first (FR-9, FR-14).

    Count-only: no order is opened and no product content is inspected. When
    there are no unknown orders, writes the sanitized no-op synchronization
    summary report (FR-28) and reports status no-op.
    """
    try:
        paths = resolve_paths()
        result = run_scan_prepare(listed_orders, paths)
    except HistorySyncError as exc:
        emit_history_error(f"history scan-prepare --listed-orders {listed_orders}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@history_app.command("scan-validate")
def history_scan_validate(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Deterministically validate an order-scan proposal."""
    try:
        result = run_scan_validate(proposal)
    except HistorySyncError as exc:
        emit_history_error(f"history scan-validate {proposal}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@history_app.command("sync-apply")
def history_sync_apply(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    listed_orders: Annotated[Path, typer.Option("--listed-orders", exists=True, dir_okay=False)],
) -> None:
    """Apply a validated order-scan proposal in one transaction.

    The --listed-orders proposal binds the scan's order identities to the
    discovered unknown set (FR-23, FR-24, FR-26).
    """
    try:
        paths = resolve_paths()
        result = run_sync_apply(proposal, paths, listed_orders_path=listed_orders)
    except HistorySyncError as exc:
        emit_history_error(f"history sync-apply {proposal}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("import-validate")
def recipes_import_validate(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Dry-run preview of a recipe-import proposal (no state change)."""
    try:
        result = run_import_validate(proposal)
    except (RecipeImportError, LockHeldError) as exc:
        emit_recipe_error(f"recipes import-validate {proposal}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("import-apply")
def recipes_import_apply(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    checksum: Annotated[
        str,
        typer.Option(
            "--checksum",
            help="The proposal checksum returned by 'recipes import-validate'.",
        ),
    ],
) -> None:
    """Apply a previewed recipe-import proposal in one transaction.

    Requires the checksum returned by 'recipes import-validate' so apply is
    bound to the exact content that was previewed (PRD 10.3.6).
    """
    try:
        paths = resolve_paths()
        result = run_import_apply(proposal, checksum, paths)
    except (RecipeImportError, LockHeldError) as exc:
        emit_recipe_error(f"recipes import-apply {proposal}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("refresh-diff")
def recipes_refresh_diff(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Produce a refresh diff preview for a recipe (FR-78)."""
    try:
        paths = resolve_paths()
        from grossy.recipes.refresh import run_refresh_diff_with_paths

        result = run_refresh_diff_with_paths(proposal, paths)
    except (RecipeRefreshError, LockHeldError) as exc:
        emit_recipe_error(f"recipes refresh-diff {proposal}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("refresh-apply")
def recipes_refresh_apply(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    checksum: Annotated[
        str,
        typer.Option(
            "--checksum",
            help="The proposal checksum returned by 'recipes refresh-diff'.",
        ),
    ],
) -> None:
    """Apply a validated recipe refresh as a new immutable revision (FR-79)."""
    try:
        paths = resolve_paths()
        result = run_refresh_apply(proposal, checksum, paths)
    except (RecipeRefreshError, LockHeldError) as exc:
        emit_recipe_error(f"recipes refresh-apply {proposal}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("discover")
def recipes_discover(
    from_classes: Annotated[
        str, typer.Option("--from", help="Comma-separated class list or 'all'.")
    ],
) -> None:
    """Report the ordered source policy for explicit recipe discovery (FR-81)."""
    try:
        result = run_recipes_discover(from_classes)
    except DiscoveryError as exc:
        emit_recipe_error(f"recipes discover --from {from_classes}", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@recipes_app.command("seed")
def recipes_seed(
    fixture_dir: Annotated[
        Path,
        typer.Option("--fixture-dir", exists=True, file_okay=False),
    ],
) -> None:
    """Import the four approved seed recipes from sanitized fixtures (FR-86..FR-88)."""
    try:
        paths = resolve_paths()
        result = run_seed_import(paths, fixture_dir)
    except (SeedImportError, LockHeldError) as exc:
        emit_recipe_error("recipes seed", exc)
    typer.echo(json.dumps(result.to_dict(), sort_keys=True))


@weekly_app.command("plan-prepare")
def weekly_plan_prepare(
    skip_history_sync: Annotated[bool, typer.Option("--skip-history-sync")] = False,
    listed_orders: Annotated[
        Path | None, typer.Option("--listed-orders", exists=True, dir_okay=False)
    ] = None,
) -> None:
    """Prepare planning with listed-order evidence or an explicit history skip."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.weekly import WeeklyPlanError, run_plan_prepare

    try:
        paths = resolve_paths()
        result = run_plan_prepare(
            paths,
            skip_history_sync=skip_history_sync,
            listed_orders_path=listed_orders,
        )
    except (WeeklyPlanError, LockHeldError, MigrationChecksumError, OSError, sqlite3.Error) as exc:
        emit_weekly_error(f"weekly plan-prepare --skip-history-sync={skip_history_sync}", exc)
    typer.echo(json.dumps(result, sort_keys=True))


@weekly_app.command("baseline-apply")
def weekly_baseline_apply(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the visible cart baseline for a weekly run."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.weekly import WeeklyPlanError, run_baseline_apply

    try:
        observation_data = json.loads(cart_observation.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        emit_weekly_error(f"weekly baseline-apply {run_id} {cart_observation}", exc)
    try:
        paths = resolve_paths()
        result = run_baseline_apply(paths, run_id, observation_data)
    except (WeeklyPlanError, LockHeldError, MigrationChecksumError, OSError, sqlite3.Error) as exc:
        emit_weekly_error(f"weekly baseline-apply {run_id} {cart_observation}", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    # PRD 10.3.1: non-zero exit for I/O failure while preserving SQLite truth.
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@weekly_app.command("recurring-preflight")
def weekly_recurring_preflight(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Re-read the cart and validate recurring additions before mutation."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.weekly.mutation import run_recurring_preflight
    from grossy.weekly.plan import WeeklyPlanError

    try:
        data = json.loads(cart_observation.read_text(encoding="utf-8"))
        result = run_recurring_preflight(resolve_paths(), run_id, data)
    except (
        ValueError,
        WeeklyPlanError,
        LockHeldError,
        MigrationChecksumError,
        OSError,
        sqlite3.Error,
    ) as exc:
        emit_weekly_error("weekly recurring-preflight", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@weekly_app.command("recurring-intent")
def weekly_recurring_intent(
    run_id: str,
    mutation_manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the recurring mutation manifest before browser mutation."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.weekly.mutation import run_recurring_intent
    from grossy.weekly.plan import WeeklyPlanError

    try:
        data = json.loads(mutation_manifest.read_text(encoding="utf-8"))
        result = run_recurring_intent(resolve_paths(), run_id, data)
    except (
        ValueError,
        WeeklyPlanError,
        LockHeldError,
        MigrationChecksumError,
        OSError,
        sqlite3.Error,
    ) as exc:
        emit_weekly_error("weekly recurring-intent", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@weekly_app.command("recurring-observe")
def weekly_recurring_observe(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Reconcile intended and observed recurring additions."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.weekly.mutation import run_recurring_observe
    from grossy.weekly.plan import WeeklyPlanError

    try:
        data = json.loads(cart_observation.read_text(encoding="utf-8"))
        result = run_recurring_observe(resolve_paths(), run_id, data)
    except (
        ValueError,
        WeeklyPlanError,
        LockHeldError,
        MigrationChecksumError,
        OSError,
        sqlite3.Error,
    ) as exc:
        emit_weekly_error("weekly recurring-observe", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@weekly_app.command("recipes-apply")
def weekly_recipes_apply(
    run_id: str,
    recipe_proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the recipe options for a weekly run."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.db.snapshot import SnapshotError
    from grossy.weekly.plan import WeeklyPlanError
    from grossy.weekly.recipes import run_recipes_apply

    try:
        data = json.loads(recipe_proposal.read_text(encoding="utf-8"))
        result = run_recipes_apply(resolve_paths(), run_id, data)
    except (
        WeeklyPlanError,
        LockHeldError,
        MigrationChecksumError,
        SnapshotError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        emit_weekly_error(f"weekly recipes-apply {run_id}", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@weekly_app.command("reroll-apply")
def weekly_reroll_apply(
    run_id: str,
    recipe_slot: str,
    from_classes: Annotated[str, typer.Option("--from")],
) -> None:
    """Reroll one recipe slot from the given classes."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.db.snapshot import SnapshotError
    from grossy.weekly.plan import WeeklyPlanError
    from grossy.weekly.recipes import run_reroll_apply

    try:
        result = run_reroll_apply(resolve_paths(), run_id, recipe_slot, from_classes)
    except (
        WeeklyPlanError,
        LockHeldError,
        MigrationChecksumError,
        SnapshotError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        emit_weekly_error(f"weekly reroll-apply {run_id} {recipe_slot}", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@weekly_app.command("selection-apply")
def weekly_selection_apply(run_id: str, recipe_list: str) -> None:
    """Persist the explicit recipe selection for a weekly run."""
    emit_not_implemented(f"weekly selection-apply {run_id} {recipe_list}")


@weekly_app.command("recipe-preflight")
def weekly_recipe_preflight(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Classify every selected recipe as passing or blocked before additions."""
    emit_not_implemented(f"weekly recipe-preflight {run_id} {cart_observation}")


@weekly_app.command("recipe-intent")
def weekly_recipe_intent(
    run_id: str,
    mutation_manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the recipe mutation manifest before browser mutation."""
    emit_not_implemented(f"weekly recipe-intent {run_id} {mutation_manifest}")


@weekly_app.command("recipe-observe")
def weekly_recipe_observe(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Reconcile intended and observed recipe additions."""
    emit_not_implemented(f"weekly recipe-observe {run_id} {cart_observation}")


@weekly_app.command("removal-preflight")
def weekly_removal_preflight(
    run_id: str,
    product: Annotated[str, typer.Option("--product")],
    quantity: Annotated[int | None, typer.Option("--quantity")] = None,
    include_preexisting: Annotated[bool, typer.Option("--include-preexisting")] = False,
) -> None:
    """Validate an explicit bounded removal before mutation."""
    if quantity is not None:
        require_positive(quantity, "--quantity")
    emit_not_implemented(
        f"weekly removal-preflight {run_id} --product {product} "
        f"--quantity={quantity} --include-preexisting={include_preexisting}"
    )


@weekly_app.command("removal-intent")
def weekly_removal_intent(
    run_id: str,
    mutation_manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the removal manifest before browser mutation."""
    emit_not_implemented(f"weekly removal-intent {run_id} {mutation_manifest}")


@weekly_app.command("removal-observe")
def weekly_removal_observe(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Reconcile intended and observed removal results."""
    emit_not_implemented(f"weekly removal-observe {run_id} {cart_observation}")


@weekly_app.command("resume-prepare")
def weekly_resume_prepare(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Reconcile persisted and live state, planning only missing positive deltas."""
    from grossy.db.migrations import MigrationChecksumError
    from grossy.weekly.mutation import run_resume_prepare
    from grossy.weekly.plan import WeeklyPlanError

    try:
        data = json.loads(cart_observation.read_text(encoding="utf-8"))
        result = run_resume_prepare(resolve_paths(), run_id, data)
    except (
        ValueError,
        WeeklyPlanError,
        LockHeldError,
        MigrationChecksumError,
        OSError,
        sqlite3.Error,
    ) as exc:
        emit_weekly_error("weekly resume-prepare", exc)
    typer.echo(json.dumps(result, sort_keys=True))
    if result.get("status") == "error":
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
