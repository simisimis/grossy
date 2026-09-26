"""SQLite connection policy for Grossy (PRD 12.1).

Every connection enables WAL mode and foreign-key enforcement.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with the Grossy connection policy.

    - WAL mode enabled (PRD 12.1.2)
    - Foreign keys enabled (PRD 12.1.1)
    - Foreign key violations are immediate
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
