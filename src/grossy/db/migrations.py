"""Deterministic SQL migration runner for Grossy (PRD 12.2).

Features:
- Versioned SQL files in ``src/grossy/migrations/`` named ``NNN_description.sql``
- SHA-256 checksums for each migration file
- Ordered application (ascending version)
- Automatic pre-migration backup before the first pending migration
- Transactional application: each migration runs in a single transaction
- Checksum mismatch stops startup
- Migration failure preserves the prior database (transaction rollback)
- No failure path deletes or recreates the database
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from grossy.db.connection import open_connection
from grossy.db.snapshot import private_sqlite_backup, verify_sqlite_snapshot
from grossy.fsutil import ensure_private_dir

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"
MIGRATION_FILE_RE = re.compile(r"^(\d{3})_.+\.sql$")


@dataclass(frozen=True)
class MigrationFile:
    version: int
    filename: str
    path: Path
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationResult:
    applied_versions: list[int]
    total_pending: int
    total_applied: int
    backup_path: Path | None


def discover_migrations(migrations_dir: Path | None = None) -> list[MigrationFile]:
    """Discover all migration files, sorted by version.

    Files must be named ``NNN_description.sql``. Unknown files are ignored.
    """
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    migrations: list[MigrationFile] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        match = MIGRATION_FILE_RE.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        content = entry.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        migrations.append(
            MigrationFile(
                version=version,
                filename=entry.name,
                path=entry,
                checksum=checksum,
                sql=content.decode("utf-8"),
            )
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_applied(conn: sqlite3.Connection) -> dict[int, str]:
    """Return {version: checksum} for already-applied migrations."""
    try:
        cursor = conn.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        # Table doesn't exist yet (fresh install).
        return {}


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )"""
    )


def run_migrations(
    db_path: Path,
    backup_dir: Path,
    migrations_dir: Path | None = None,
) -> MigrationResult:
    """Run all pending migrations on the database.

    Before the first pending migration is applied, an automatic backup of the
    current database is created in ``backup_dir``. Each migration is applied
    in a transaction; failure rolls back that migration without affecting
    prior state.

    Returns a :class:`MigrationResult` summarizing what happened.
    """
    all_migrations = discover_migrations(migrations_dir)
    if not all_migrations:
        return MigrationResult([], 0, 0, None)

    # Use isolation_level=None for explicit transaction control in migrations.
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # The backup that this specific attempt created and verified. It stays
    # None until after verify_sqlite_snapshot succeeds, so callers can report
    # a truthful, attempt-bound restore point instead of scanning the backup
    # directory for unrelated or unverified files (PRD 12.2.4, 18).
    verified_backup: Path | None = None
    try:
        _ensure_migrations_table(conn)
        applied = _get_applied(conn)

        # Verify checksums of already-applied migrations.
        for mig in all_migrations:
            if mig.version in applied and applied[mig.version] != mig.checksum:
                raise MigrationChecksumError(
                    mig.version,
                    mig.filename,
                    applied[mig.version],
                    mig.checksum,
                )

        pending = [m for m in all_migrations if m.version not in applied]
        if not pending:
            return MigrationResult([], 0, len(all_migrations), None)

        # Create and VERIFY the pre-migration backup before the first pending
        # migration (PRD 12.2.4). If the snapshot fails verification the run
        # aborts before any migration is applied, leaving the prior database
        # usable (PRD 12.2.7, 18).
        backup_path = (
            backup_dir / f"pre-migration-{_now_utc().replace(':', '-').replace('.', '-')}.sqlite3"
        )
        ensure_private_dir(backup_path.parent)
        _backup_database(conn, backup_path)
        verify_sqlite_snapshot(backup_path)
        verified_backup = backup_path

        applied_now: list[int] = []
        for mig in pending:
            try:
                _apply_migration(conn, mig)
                applied_now.append(mig.version)
            except Exception:
                raise

        return MigrationResult(
            applied_versions=applied_now,
            total_pending=len(pending),
            total_applied=len(applied) + len(applied_now),
            backup_path=backup_path,
        )
    except Exception as exc:
        # Attach the attempt-bound verified backup (None until created and
        # verified) to the propagating exception so a caller can report the
        # restore point for *this* migration attempt only. A checksum mismatch
        # (which aborts before any backup) and a failed backup verification
        # both leave verified_backup as None.
        exc.verified_backup_path = verified_backup  # type: ignore[attr-defined]
        raise
    finally:
        conn.close()


class MigrationChecksumError(Exception):
    """Raised when an applied migration's checksum does not match the file."""

    def __init__(self, version: int, filename: str, expected: str, actual: str) -> None:
        self.version = version
        self.filename = filename
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"migration checksum mismatch for version {version} ({filename}): "
            f"expected {expected[:16]}..., got {actual[:16]}..."
        )


def _is_pure_comment(text: str) -> bool:
    return all(
        stripped_line.startswith("--") or not stripped_line for stripped_line in text.split("\n")
    )


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual complete statements.

    Uses ``sqlite3.complete_statement()`` which correctly handles triggers
    (BEGIN...END blocks with embedded semicolons), comments, and multi-line
    statements.
    """
    import sqlite3 as _sqlite3

    statements: list[str] = []
    current = ""
    for line in sql.split("\n"):
        current += line + "\n"
        if _sqlite3.complete_statement(current):
            stmt = current.strip()
            if stmt and not _is_pure_comment(stmt):
                statements.append(stmt)
            current = ""
    if current.strip():
        stmt = current.strip()
        if stmt and not _is_pure_comment(stmt):
            statements.append(stmt)
    return statements


def _apply_migration(conn: sqlite3.Connection, mig: MigrationFile) -> None:
    """Apply a single migration in a transaction.

    Each SQL statement in the migration is executed individually within
    a single transaction. On failure, the transaction is rolled back.

    Migrations whose SQL begins with the ``-- FK_OFF`` marker are applied
    with ``PRAGMA foreign_keys=OFF`` so that table-rebuild migrations can
    safely drop and recreate referenced tables (PRD 12.2).
    """
    fk_off = mig.sql.lstrip().startswith("-- FK_OFF")
    if fk_off:
        conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        statements = _split_sql_statements(mig.sql)
        for statement in statements:
            conn.execute(statement)

        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (mig.version, mig.filename, mig.checksum, _now_utc()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        if fk_off:
            conn.execute("PRAGMA foreign_keys=ON")


def _backup_database(conn: sqlite3.Connection, dest: Path) -> None:
    """Create a SQLite-safe backup using the built-in backup API."""
    private_sqlite_backup(conn, dest)


def get_schema_version(db_path: Path) -> int:
    """Return the current schema version (highest applied migration)."""
    conn = open_connection(db_path)
    try:
        try:
            cursor = conn.execute("SELECT MAX(version) FROM schema_migrations")
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()
