"""Backup, export, and restore operations for Grossy (PRD section 14).

- Backup: SQLite-safe backup with manifest and checksum (PRD 14.1)
- Export: versioned private JSON export (PRD 14.2)
- Restore: validated, confirmed, atomic restore (PRD 14.3)

Restore safety model (PRD 14.3, 18):

- The writer lock is acquired first and held across the no-active-run check,
  validation, and activation so a concurrent writer cannot start a run in the
  gap between check and replacement.
- A backup is only restored when it ships a complete, supported manifest whose
  checksum exactly matches the file and whose declared schema version matches
  the backup's actual schema version and is not newer than the installed
  application's maximum supported migration.
- The current database is backed up and the snapshot verified (integrity,
  foreign keys, checksum) before the database is ever replaced.
- Any validation or I/O failure preserves the existing database (and export
  destination) untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from grossy.db.connection import open_connection
from grossy.db.migrations import discover_migrations, get_schema_version
from grossy.db.snapshot import SnapshotError, verify_sqlite_snapshot
from grossy.fsutil import atomic_write

BACKUP_MANIFEST_SCHEMA = "grossy.db.backup-manifest"
BACKUP_MANIFEST_VERSION = 1
EXPORT_SCHEMA = "grossy.db.export"
EXPORT_VERSION = 1

# Tables included in the export (PRD 14.2: history, products, families,
# recurrence, overrides, blacklists, recipe revisions, run records).
# These cover all observable purchase history, learned state, and run state.
# Table names are trusted internal identifiers (never user input). A table
# that is required at the database's declared schema version must exist;
# its absence indicates a damaged database and aborts the export. Only tables
# not yet introduced at that schema version may be exported as empty.
_EXPORT_TABLES = (
    "source_orders",
    "source_order_products",
    "products",
    "categories",
    "product_families",
    "family_members",
    "manual_overrides",
    "blacklist_events",
    "recipes",
    "recipe_revisions",
    "recipe_ingredients",
    "recipe_appliance_requirements",
    "recipe_variants",
    "recipe_events",
    "planner_runs",
    "run_cart_baselines",
    "run_recurring_items",
    "run_recipe_slots",
    "run_rerolls",
    "run_recipe_plans",
    "run_recipe_seen",
    "run_recipe_selections",
    "run_ingredient_targets",
    "run_mutation_manifests",
    "run_mutation_observations",
    "run_removals",
    "run_events",
)


class RestoreError(Exception):
    """Raised when restore validation or execution fails."""


class ExportError(Exception):
    """Raised when a database export cannot be produced safely."""


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _manifest_path_for(backup_path: Path) -> Path:
    return backup_path.with_suffix(backup_path.suffix + ".manifest.json")


def _verify_snapshot(path: Path) -> tuple[str, int]:
    """Verify a snapshot, translating :class:`SnapshotError` to RestoreError."""
    try:
        return verify_sqlite_snapshot(path)
    except SnapshotError as exc:
        raise RestoreError(str(exc)) from exc


def _create_verified_backup(source: Path, dest_dir: Path, prefix: str) -> Path:
    """Create a SQLite-safe backup and verify it before relying on it.

    The snapshot is integrity/foreign-key checked and its checksum computed
    before the caller proceeds (PRD 12.2.4, 14.3.5). The backup is mode 0600.
    """
    dest = dest_dir / f"{prefix}-{_now_utc().replace(':', '-').replace('.', '-')}.sqlite3"
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_conn = open_connection(source)
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    dest.chmod(0o600)
    # Verify the snapshot before the caller uses it to proceed.
    _verify_snapshot(dest)
    return dest


# --- Backup (PRD 14.1) -----------------------------------------------------


def db_backup(db_path: Path, dest_path: Path) -> Path:
    """Create a SQLite-safe backup with a schema-version manifest and checksum.

    The backup file uses SQLite's built-in backup API (consistent snapshot) and
    is verified before the manifest is written. A JSON manifest file
    (``<dest>.manifest.json``) is written alongside with the schema version and
    checksum. Both files are mode 0600.
    """
    if not db_path.exists():
        raise RestoreError(f"database not found: {db_path}; run init first")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    src_conn = open_connection(db_path)
    dst_conn = sqlite3.connect(str(dest_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    dest_path.chmod(0o600)

    # Verify the snapshot, then record its checksum and schema version.
    checksum, schema_version = _verify_snapshot(dest_path)

    manifest = {
        "schema_name": BACKUP_MANIFEST_SCHEMA,
        "schema_version_manifest": BACKUP_MANIFEST_VERSION,
        "database_schema_version": schema_version,
        "checksum_sha256": checksum,
        "backup_at": _now_utc(),
        "source_database": str(db_path),
    }
    manifest_path = _manifest_path_for(dest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)

    return dest_path


# --- Export (PRD 14.2) -----------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _required_export_tables(schema_version: int) -> frozenset[str]:
    """Return the set of export tables that must exist at the given schema version.

    Recipe planning snapshots and the seen ledger were introduced by migration
    009; the remaining inventory dates to migration 001. Missing tables are
    corruption only when the declared schema requires them (PRD 14.2, 18).
    """
    if schema_version < 1:
        return frozenset()
    recipe_planning_tables = {"run_recipe_plans", "run_recipe_seen"}
    if schema_version < 9:
        return frozenset(_EXPORT_TABLES) - recipe_planning_tables
    return frozenset(_EXPORT_TABLES)


def _table_rows(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f"SELECT * FROM {name}")
    try:
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()
    return rows


def db_export(db_path: Path, dest_path: Path) -> Path:
    """Write a versioned private JSON export (mode 0600).

    Includes all history, product, family, recipe, and run data. Excludes
    credentials, cookies, browser state, and configuration secrets (these are
    not stored in the database by design).

    A table that is required at the database's declared schema version must
    exist; its absence indicates a damaged database and raises
    :class:`ExportError` before the destination is touched (PRD 14.2, 18).
    Only tables not yet introduced at that schema version are exported as an
    empty list. Any real query, I/O, or lock failure also aborts the export
    before the destination is touched, so a pre-existing destination is
    preserved.
    """
    if not db_path.exists():
        raise ExportError(f"database not found: {db_path}; run init first")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    conn = open_connection(db_path)
    try:
        schema_version = get_schema_version(db_path)
        if schema_version < 1:
            raise ExportError(
                f"database schema version {schema_version} is not a valid initialized "
                "database; cannot export an empty or uninitialized database (PRD 14.2)"
            )
        required = _required_export_tables(schema_version)
        data: dict[str, Any] = {}
        for table in _EXPORT_TABLES:
            if _table_exists(conn, table):
                data[table] = _table_rows(conn, table)
            elif table in required:
                raise ExportError(
                    f"required table {table!r} is missing from a schema version "
                    f"{schema_version} database; the database is damaged and "
                    "cannot be exported safely (PRD 14.2, 18)"
                )
            else:
                # Not yet introduced at this schema version; export as empty.
                data[table] = []
    finally:
        conn.close()

    export = {
        "schema_name": EXPORT_SCHEMA,
        "schema_version": EXPORT_VERSION,
        "database_schema_version": schema_version,
        "exported_at": _now_utc(),
        "data": data,
    }
    content = json.dumps(export, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    # Atomic write: the destination is only replaced once every table was read
    # successfully; a mid-export failure (including a missing required table)
    # leaves any existing file untouched.
    atomic_write(dest_path, content.encode("utf-8"))
    return dest_path


# --- Restore (PRD 14.3) -----------------------------------------------------


@dataclass(frozen=True)
class RestoreInfo:
    """Information gathered during restore validation for confirmation display."""

    source_schema_version: int
    target_schema_version: int
    source_checksum: str
    backup_path: Path


def _max_supported_schema_version() -> int:
    """Highest schema version the installed application's migrations support."""
    return max((m.version for m in discover_migrations()), default=0)


def _require_no_active_run(db_path: Path) -> None:
    """Raise :class:`RestoreError` if a non-terminal weekly run exists (PRD 14.3.1)."""
    if not db_path.exists():
        return
    conn = open_connection(db_path)
    try:
        try:
            active = conn.execute(
                "SELECT run_id FROM planner_runs WHERE lifecycle NOT IN ('completed', 'abandoned')"
            ).fetchone()
        except sqlite3.OperationalError:
            # planner_runs absent (pre-schema database); nothing to block on.
            return
        if active is not None:
            raise RestoreError(
                f"cannot restore: active weekly run {active[0]!r} exists; "
                "finish or abandon it first"
            )
    finally:
        conn.close()


def _load_backup_manifest(backup_path: Path) -> dict[str, Any]:
    """Load and validate a complete, supported backup manifest (PRD 14.3.3).

    A backup without a manifest, or with an unsupported schema, a missing or
    malformed checksum, or an invalid schema version is rejected.
    """
    manifest_path = _manifest_path_for(backup_path)
    if not manifest_path.exists():
        raise RestoreError(
            f"backup manifest not found: {manifest_path}; "
            "a supported backup must ship a manifest (PRD 14.3.3)"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RestoreError(f"backup manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RestoreError("backup manifest must be a JSON object")
    if manifest.get("schema_name") != BACKUP_MANIFEST_SCHEMA:
        raise RestoreError(f"invalid manifest schema_name: {manifest.get('schema_name')!r}")
    if manifest.get("schema_version_manifest") != BACKUP_MANIFEST_VERSION:
        raise RestoreError(
            "unsupported manifest schema_version_manifest: "
            f"{manifest.get('schema_version_manifest')!r}"
        )
    checksum = manifest.get("checksum_sha256")
    if not isinstance(checksum, str) or not _is_sha256_hex(checksum):
        raise RestoreError(
            "backup manifest is missing a usable checksum_sha256; "
            "a complete manifest is required (PRD 14.3.3)"
        )
    version = manifest.get("database_schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RestoreError(
            f"backup manifest is missing a valid database_schema_version: {version!r}"
        )
    return manifest


def db_restore(
    db_path: Path,
    backup_path: Path,
    state_dir: Path,
    confirm: bool = False,
) -> RestoreInfo:
    """Restore a verified backup under PRD 14.3 constraints.

    1. Acquire the writer lock and hold it across the whole operation.
    2. Require no active weekly run (checked under the lock).
    3. Validate the manifest, exact checksum, SQLite integrity, foreign keys,
       and that the manifest version matches the backup's actual schema version.
    4. Reject a schema newer than the installed application's maximum.
    5. Create and verify a backup of the current database.
    6. Show the restore source and target schema versions.
    7. Require explicit confirmation (``confirm=True``).
    8. Restore atomically, re-verifying before activation.
    9. Preserve the prior database and any existing export/backup on failure.

    Returns :class:`RestoreInfo`. Raises :class:`RestoreError` on any failure.
    """
    from grossy.db.lock import WriterLock

    # 1. Acquire the writer lock first (PRD 14.3.2) and hold it across the
    #    no-active-run check, validation, and activation so a concurrent writer
    #    cannot start a run or otherwise race the replacement.
    with WriterLock(state_dir):
        # 2. No active weekly run (PRD 14.3.1), rechecked under the lock.
        _require_no_active_run(db_path)

        # 3. The backup file must exist and ship a complete manifest.
        if not backup_path.exists():
            raise RestoreError(f"backup file not found: {backup_path}")
        manifest = _load_backup_manifest(backup_path)

        # Exact checksum match (PRD 14.3.3), checked before opening the file so
        # a tampered backup is rejected for the wrong checksum, not a vague
        # integrity error.
        manifest_checksum = manifest["checksum_sha256"]
        snapshot_checksum = _sha256_file(backup_path)
        if snapshot_checksum != manifest_checksum:
            raise RestoreError(
                f"backup checksum mismatch: manifest has {manifest_checksum[:16]}..., "
                f"file has {snapshot_checksum[:16]}..."
            )

        # Now that the checksum matches, verify integrity, foreign keys, and the
        # backup's actual schema version.
        _, actual_schema_version = _verify_snapshot(backup_path)

        # Manifest must agree with the backup's actual schema (PRD 14.3.3).
        manifest_version = manifest["database_schema_version"]
        if manifest_version != actual_schema_version:
            raise RestoreError(
                f"backup manifest schema version {manifest_version} disagrees with "
                f"the backup's actual schema version {actual_schema_version}"
            )

        # 4. Reject a schema newer than the installed application's maximum.
        max_supported = _max_supported_schema_version()
        if actual_schema_version > max_supported:
            raise RestoreError(
                f"backup schema version {actual_schema_version} is newer than the "
                f"maximum supported version {max_supported}; upgrade Grossy first"
            )

        # 6. Gather info for the confirmation gate.
        target_schema_version = get_schema_version(db_path) if db_path.exists() else 0
        info = RestoreInfo(
            source_schema_version=actual_schema_version,
            target_schema_version=target_schema_version,
            source_checksum=snapshot_checksum,
            backup_path=backup_path,
        )
        if not confirm:
            return info

        # 5. Create and verify a backup of the current database (PRD 14.3.5).
        if db_path.exists():
            _create_verified_backup(db_path, db_path.parent, "pre-restore")

        # 8/9. Activate atomically with a final pre-activation verification.
        _verify_snapshot(backup_path)
        tmp_path = db_path.with_suffix(".restore-tmp")
        tmp_path.write_bytes(backup_path.read_bytes())
        tmp_path.chmod(0o600)
        _verify_snapshot(tmp_path)
        tmp_path.replace(db_path)
        # Remove stale WAL/SHM side files from the previous database.
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                side.unlink()

    return info
