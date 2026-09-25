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
from pathlib import Path
from typing import Annotated, NoReturn

import typer

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


def require_positive(value: int, name: str) -> None:
    if value <= 0:
        typer.echo(f"grossy: {name} must be a positive integer, got {value}", err=True)
        raise typer.Exit(code=2)


def require_positive_decimal(value: float, name: str) -> None:
    if value <= 0:
        typer.echo(f"grossy: {name} must be a positive decimal, got {value}", err=True)
        raise typer.Exit(code=2)


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
    emit_not_implemented("init")


@app.command()
def doctor() -> None:
    """Validate the local environment and authenticated read-only AB.gr access."""
    emit_not_implemented("doctor")


@history_app.command("set-primary")
def history_set_primary(family_id: str, product_id: str) -> None:
    """Explicitly set the primary product of an established family."""
    emit_not_implemented(f"history set-primary {family_id} {product_id}")


@history_app.command("set-recurrence")
def history_set_recurrence(
    family_id: str,
    weeks: Annotated[int, typer.Option("--weeks")],
) -> None:
    """Explicitly set a family recurrence interval in whole weeks."""
    require_positive(weeks, "--weeks")
    emit_not_implemented(f"history set-recurrence {family_id} --weeks {weeks}")


@history_app.command("set-quantity")
def history_set_quantity(product_or_family_id: str, quantity: int) -> None:
    """Explicitly set a typical quantity for a product or family."""
    require_positive(quantity, "quantity")
    emit_not_implemented(f"history set-quantity {product_or_family_id} {quantity}")


@history_app.command("disable")
def history_disable(family_id: str) -> None:
    """Disable a family, suppressing history-driven recurring prefill."""
    emit_not_implemented(f"history disable {family_id}")


@history_app.command("enable")
def history_enable(family_id: str) -> None:
    """Re-enable a disabled family."""
    emit_not_implemented(f"history enable {family_id}")


@blacklist_app.command("product")
def history_blacklist_product(product_id: str) -> None:
    """Block an exact product from all automatic selection."""
    emit_not_implemented(f"history blacklist product {product_id}")


@blacklist_app.command("family")
def history_blacklist_family(family_id: str) -> None:
    """Block every member of a family everywhere."""
    emit_not_implemented(f"history blacklist family {family_id}")


@unblacklist_app.command("product")
def history_unblacklist_product(product_id: str) -> None:
    """Remove an explicit product blacklist."""
    emit_not_implemented(f"history unblacklist product {product_id}")


@unblacklist_app.command("family")
def history_unblacklist_family(family_id: str) -> None:
    """Remove an explicit family blacklist."""
    emit_not_implemented(f"history unblacklist family {family_id}")


@family_app.command("link")
def history_family_link(product_id: str, family_id: str) -> None:
    """Link an exact product into a family."""
    emit_not_implemented(f"history family link {product_id} {family_id}")


@family_app.command("unlink")
def history_family_unlink(product_id: str) -> None:
    """Remove a product from its family."""
    emit_not_implemented(f"history family unlink {product_id}")


@family_app.command("merge")
def history_family_merge(source_family_id: str, target_family_id: str) -> None:
    """Merge one family into another."""
    emit_not_implemented(f"history family merge {source_family_id} {target_family_id}")


@history_app.command("set-category")
def history_set_category(product_or_family_id: str, category: str) -> None:
    """Explicitly assign a normalized category."""
    emit_not_implemented(f"history set-category {product_or_family_id} {category}")


@category_app.command("rename")
def history_category_rename(old_name: str, new_name: str) -> None:
    """Rename a normalized category transactionally."""
    emit_not_implemented(f"history category rename {old_name} {new_name}")


@recipes_app.command("add-file")
def recipes_add_file(
    recipe_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Import a user-authored YAML recipe."""
    emit_not_implemented(f"recipes add-file {recipe_file}")


@recipes_app.command("favorite")
def recipes_favorite(recipe_id: str) -> None:
    """Mark a recipe as favorite (base weight 3)."""
    emit_not_implemented(f"recipes favorite {recipe_id}")


@recipes_app.command("regular")
def recipes_regular(recipe_id: str) -> None:
    """Mark a recipe as regular (base weight 1)."""
    emit_not_implemented(f"recipes regular {recipe_id}")


@recipes_app.command("weight")
def recipes_weight(recipe_id: str, weight: float) -> None:
    """Set an explicit base weight for a recipe."""
    require_positive_decimal(weight, "weight")
    emit_not_implemented(f"recipes weight {recipe_id} {weight}")


@recipes_app.command("enable")
def recipes_enable(recipe_id: str) -> None:
    """Enable a recipe."""
    emit_not_implemented(f"recipes enable {recipe_id}")


@recipes_app.command("disable")
def recipes_disable(recipe_id: str) -> None:
    """Disable a recipe."""
    emit_not_implemented(f"recipes disable {recipe_id}")


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
    emit_not_implemented(f"db backup {path}")


@db_app.command("export")
def db_export(path: Path) -> None:
    """Write a versioned JSON export."""
    emit_not_implemented(f"db export {path}")


@db_app.command("restore")
def db_restore(backup: Path) -> None:
    """Restore a verified backup under the PRD restore constraints."""
    emit_not_implemented(f"db restore {backup}")


# --- PRD 10.2: deterministic workflow commands ----------------------------


@history_app.command("discover-prepare")
def history_discover_prepare() -> None:
    """Prepare the observable-six listed-order discovery phase."""
    emit_not_implemented("history discover-prepare")


@history_app.command("sync-dry-run")
def history_sync_dry_run(
    listed_orders: Annotated[Path, typer.Option("--listed-orders", exists=True, dir_okay=False)],
) -> None:
    """Report only the count of unknown listed source order IDs."""
    emit_not_implemented(f"history sync-dry-run --listed-orders {listed_orders}")


@history_app.command("scan-validate")
def history_scan_validate(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Deterministically validate an order-scan proposal."""
    emit_not_implemented(f"history scan-validate {proposal}")


@history_app.command("sync-apply")
def history_sync_apply(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Apply a validated order-scan proposal in one transaction."""
    emit_not_implemented(f"history sync-apply {proposal}")


@recipes_app.command("import-validate")
def recipes_import_validate(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Deterministically validate a recipe-import proposal."""
    emit_not_implemented(f"recipes import-validate {proposal}")


@recipes_app.command("import-apply")
def recipes_import_apply(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Apply a validated recipe-import proposal."""
    emit_not_implemented(f"recipes import-apply {proposal}")


@recipes_app.command("refresh-diff")
def recipes_refresh_diff(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Produce a refresh diff preview for a recipe."""
    emit_not_implemented(f"recipes refresh-diff {proposal}")


@recipes_app.command("refresh-apply")
def recipes_refresh_apply(
    proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Apply a validated recipe refresh as a new revision."""
    emit_not_implemented(f"recipes refresh-apply {proposal}")


@weekly_app.command("plan-prepare")
def weekly_plan_prepare(
    skip_history_sync: Annotated[bool, typer.Option("--skip-history-sync")] = False,
) -> None:
    """Prepare a weekly run in the planning lifecycle state."""
    emit_not_implemented(f"weekly plan-prepare --skip-history-sync={skip_history_sync}")


@weekly_app.command("baseline-apply")
def weekly_baseline_apply(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the visible cart baseline for a weekly run."""
    emit_not_implemented(f"weekly baseline-apply {run_id} {cart_observation}")


@weekly_app.command("recurring-preflight")
def weekly_recurring_preflight(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Re-read the cart and validate recurring additions before mutation."""
    emit_not_implemented(f"weekly recurring-preflight {run_id} {cart_observation}")


@weekly_app.command("recurring-intent")
def weekly_recurring_intent(
    run_id: str,
    mutation_manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the recurring mutation manifest before browser mutation."""
    emit_not_implemented(f"weekly recurring-intent {run_id} {mutation_manifest}")


@weekly_app.command("recurring-observe")
def weekly_recurring_observe(
    run_id: str,
    cart_observation: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Reconcile intended and observed recurring additions."""
    emit_not_implemented(f"weekly recurring-observe {run_id} {cart_observation}")


@weekly_app.command("recipes-apply")
def weekly_recipes_apply(
    run_id: str,
    recipe_proposal: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Persist the recipe options for a weekly run."""
    emit_not_implemented(f"weekly recipes-apply {run_id} {recipe_proposal}")


@weekly_app.command("reroll-apply")
def weekly_reroll_apply(
    run_id: str,
    recipe_slot: str,
    from_classes: Annotated[str, typer.Option("--from")],
) -> None:
    """Reroll one recipe slot from the given classes."""
    emit_not_implemented(f"weekly reroll-apply {run_id} {recipe_slot} --from {from_classes}")


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
    emit_not_implemented(f"weekly resume-prepare {run_id} {cart_observation}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
