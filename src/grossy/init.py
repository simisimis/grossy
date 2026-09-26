"""``grossy init`` implementation (PRD FR-1, FR-2, sections 12, 13).

Creates XDG directories, sample configuration, initial database with
migrations applied, process-lock location, and required permissions.
Refuses to overwrite an existing configuration or database.
"""

from __future__ import annotations

from grossy.db.lock import WriterLock
from grossy.db.migrations import get_schema_version, run_migrations
from grossy.fsutil import FILE_MODE, atomic_write_text, ensure_private_dir
from grossy.paths import GrossyPaths

SAMPLE_CONFIG = """\
schema_version: 1
retailer: ab
timezone: Europe/Athens
currency: EUR

household:
  adults: 2
  children: 1
  recipe_servings: 3

weekly_budget: 100
material_unit_price_improvement_pct: 15

disliked_ingredients: []

pantry_assumptions:
  - salt
  - black pepper
  - olive oil

appliances:
  thermomix:
    available: true
    model: null
  dough_kneading_machine:
    available: true
  steam_oven:
    available: true
  air_fryer:
    available: true
    capacity_litres: null
  induction_cooktop:
    available: true
  microwave:
    available: true
    wattage: null
  pressure_cooker:
    available: true
    kind: null
    capacity_litres: null
  dutch_oven:
    available: true
    capacity_litres: null

appliance_preferences:
  prefer_air_fryer_over_pan_frying: true
  prefer_thermomix_native_over_manual: true

staples:
  breakfast: []
  lunch: []
"""


class InitError(Exception):
    """Raised when grossy init cannot proceed."""


def do_init(paths: GrossyPaths) -> dict[str, object]:
    """Execute the grossy init procedure.

    Creates:
    - config directory (0700) with sample config.yaml (0600)
    - data directory (0700) with grossy.sqlite3 (0600)
    - backups directory (0700)
    - state directory (0700) with writer lock location
    - browser profile directory (0700)

    Fails if config or database already exists (non-overwrite, PRD FR-2).
    """
    # FR-2: Refuse to overwrite existing config or database.
    if paths.config_file.exists():
        raise InitError(
            f"configuration already exists at {paths.config_file}; "
            "refusing to overwrite (use existing config or remove manually)"
        )
    if paths.database_file.exists():
        raise InitError(
            f"database already exists at {paths.database_file}; "
            "refusing to overwrite (use existing database or restore a backup)"
        )

    # Create directories with 0700 policy.
    ensure_private_dir(paths.config_dir)
    ensure_private_dir(paths.data_dir)
    ensure_private_dir(paths.backups_dir)
    ensure_private_dir(paths.state_dir)
    ensure_private_dir(paths.browser_profile_dir)

    # Write sample configuration (0600).
    atomic_write_text(paths.config_file, SAMPLE_CONFIG)

    # Initialize database with migrations under the writer lock.
    with WriterLock(paths.state_dir):
        result = run_migrations(paths.database_file, paths.backups_dir)

    # Enforce 0600 on the database file.
    if paths.database_file.exists():
        paths.database_file.chmod(FILE_MODE)

    schema_version = get_schema_version(paths.database_file)

    return {
        "schema_name": "grossy.init.result",
        "schema_version": 1,
        "status": "success",
        "config_path": str(paths.config_file),
        "database_path": str(paths.database_file),
        "database_schema_version": schema_version,
        "migrations_applied": result.applied_versions,
        "backups_dir": str(paths.backups_dir),
        "state_dir": str(paths.state_dir),
    }
