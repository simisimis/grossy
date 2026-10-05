"""Shared SQLite snapshot verification (PRD 12.2.4, 14.3.5, 14.3.9).

A snapshot is a file that should be a self-consistent SQLite database. Both the
automatic pre-migration backup (PRD 12.2.4) and the pre-restore backup
(PRD 14.3.5) must be verified before the operation proceeds, and a backup being
restored is integrity/foreign-key checked before activation (PRD 14.3.9).

This module has no dependency on other ``grossy.db`` submodules so it can be
imported from both :mod:`grossy.db.backup` and :mod:`grossy.db.migrations`
without a circular import.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path


class SnapshotError(Exception):
    """Raised when a SQLite snapshot is not a valid, consistent database."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sqlite_snapshot(path: Path) -> tuple[str, int]:
    """Verify a SQLite snapshot and return ``(checksum_sha256, schema_version)``.

    Runs an integrity check and a foreign-key check and reads the highest
    applied migration version. Raises :class:`SnapshotError` if the snapshot is
    not a valid, consistent SQLite database.
    """
    checksum = _sha256_file(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise SnapshotError(f"snapshot integrity check failed: {row}")
        conn.execute("PRAGMA foreign_keys=ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SnapshotError(f"snapshot has foreign key violations: {violations[:5]}")
        try:
            version_row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            version_row = None
    finally:
        conn.close()
    schema_version = version_row[0] if version_row and version_row[0] is not None else 0
    return checksum, schema_version


def private_sqlite_backup(conn: sqlite3.Connection, dest: Path) -> None:
    """Publish a consistent snapshot without a world-readable creation window."""
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    os.close(fd)
    temp = Path(name)
    try:
        target = sqlite3.connect(str(temp))
        try:
            conn.backup(target)
            # Snapshots are standalone files; never publish WAL sidecars.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
        verify_sqlite_snapshot(temp)
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            temp.with_name(temp.name + suffix).unlink(missing_ok=True)
