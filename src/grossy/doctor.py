"""Deterministic grossy doctor diagnostics (PRD FR-3, FR-4, FR-5).

The doctor validates the local environment and, when a browser adapter is
provided, authenticated read-only AB.gr access. It is strictly read-only:
it never mutates the database, configuration, or cart. Missing authentication
yields manual-login instructions, never automated credential entry.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass

from grossy.browser.adapter import BrowserAdapter
from grossy.config import GrossyConfigError, load_config_file
from grossy.db.lock import LOCK_FILE_NAME
from grossy.db.migrations import discover_migrations
from grossy.fsutil import DIR_MODE, FILE_MODE
from grossy.paths import GrossyPaths
from grossy.security import sanitize_human_text

DOCTOR_SCHEMA_NAME = "grossy.doctor.result"
DOCTOR_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DoctorCheckResult:
    """Result of a single doctor check."""

    name: str
    status: str  # "pass" | "fail" | "warn" | "skipped"
    detail: str
    remediation: str | None = None


@dataclass(frozen=True)
class DoctorResult:
    """Aggregate result of all doctor checks."""

    status: str  # "pass" | "fail" | "warn"
    checks: tuple[DoctorCheckResult, ...]

    @property
    def has_failures(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return self.status == "warn" or any(c.status in ("warn", "skipped") for c in self.checks)

    def to_dict(self) -> dict[str, object]:
        checks_out: list[dict[str, object]] = []
        for c in self.checks:
            entry: dict[str, object] = {
                "name": c.name,
                "status": c.status,
                "detail": sanitize_human_text(c.detail),
            }
            if c.remediation is not None:
                entry["remediation"] = sanitize_human_text(c.remediation)
            checks_out.append(entry)
        return {
            "schema_name": DOCTOR_SCHEMA_NAME,
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "status": self.status,
            "checks": checks_out,
        }


def run_doctor(paths: GrossyPaths, browser: BrowserAdapter | None = None) -> DoctorResult:
    """Run all doctor checks and return an aggregate result.

    Static checks (config, database, migrations, permissions, tools) always
    run. Browser-dependent checks (auth, history controls, cart read) run
    only when a browser adapter is provided; otherwise they are reported
    as skipped and the overall status is "warn" (not "pass") because the
    required FR-3 checks could not be completed.
    """
    checks: list[DoctorCheckResult] = [
        _check_config(paths),
        _check_database(paths),
        _check_migrations(paths),
        _check_foreign_keys(paths),
        _check_wal_mode(paths),
        _check_permissions(paths),
        _check_process_lock(paths),
        _check_brave(),
        _check_agent_browser(),
        _check_browser_profile(paths),
    ]
    if browser is not None:
        checks.append(_check_auth(browser))
        checks.append(_check_history_controls(browser))
        checks.append(_check_cart_read(browser))
    else:
        checks.append(
            DoctorCheckResult(
                "auth",
                "skipped",
                "no browser adapter; authenticated checks require task 14 user verification",
            )
        )
        checks.append(
            DoctorCheckResult(
                "history_controls",
                "skipped",
                "no browser adapter; authenticated checks require task 14 user verification",
            )
        )
        checks.append(
            DoctorCheckResult(
                "cart_read",
                "skipped",
                "no browser adapter; authenticated checks require task 14 user verification",
            )
        )
    if any(c.status == "fail" for c in checks):
        status = "fail"
    elif any(c.status in ("warn", "skipped") for c in checks):
        status = "warn"
    else:
        status = "pass"
    return DoctorResult(status=status, checks=tuple(checks))


# --- individual checks -------------------------------------------------------


def _check_config(paths: GrossyPaths) -> DoctorCheckResult:
    if not paths.config_file.exists():
        return DoctorCheckResult(
            "config",
            "fail",
            f"configuration not found at {paths.config_file}",
            "run 'grossy init' to create the configuration",
        )
    try:
        load_config_file(paths.config_file)
        return DoctorCheckResult("config", "pass", "configuration valid")
    except GrossyConfigError as exc:
        return DoctorCheckResult(
            "config",
            "fail",
            str(exc),
            "fix the configuration file",
        )
    except Exception as exc:
        return DoctorCheckResult(
            "config",
            "fail",
            f"cannot read configuration: {exc}",
        )


def _check_database(paths: GrossyPaths) -> DoctorCheckResult:
    if not paths.database_file.exists():
        return DoctorCheckResult(
            "database",
            "fail",
            f"database not found at {paths.database_file}",
            "run 'grossy init' to create the database",
        )
    all_migs = discover_migrations()
    supported_max = max((m.version for m in all_migs), default=None)
    try:
        conn = sqlite3.connect(f"file:{paths.database_file}?mode=ro", uri=True)
        try:
            try:
                cursor = conn.execute("SELECT MAX(version) FROM schema_migrations")
                row = cursor.fetchone()
                version = row[0] if row and row[0] is not None else 0
            except sqlite3.OperationalError:
                version = 0
            if version == 0:
                return DoctorCheckResult(
                    "database",
                    "warn",
                    "database exists but has no applied migrations",
                    "run 'grossy init' to apply migrations",
                )
            if supported_max is None:
                return DoctorCheckResult(
                    "database",
                    "warn",
                    f"schema version {version}; no local migration set to validate against",
                )
            if version > supported_max:
                return DoctorCheckResult(
                    "database",
                    "fail",
                    f"schema version {version} is newer than the locally "
                    f"supported version {supported_max}",
                    "database was created by a newer grossy version; restore a "
                    "compatible backup or use the matching grossy version",
                )
            return DoctorCheckResult(
                "database",
                "pass",
                f"schema version {version} (supported up to {supported_max})",
            )
        finally:
            conn.close()
    except Exception as exc:
        return DoctorCheckResult(
            "database",
            "fail",
            f"database cannot be opened: {exc}",
            "restore a backup or re-initialize",
        )


def _check_migrations(paths: GrossyPaths) -> DoctorCheckResult:
    if not paths.database_file.exists():
        return DoctorCheckResult("migrations", "skipped", "database not present")
    try:
        all_migs = discover_migrations()
        if not all_migs:
            return DoctorCheckResult("migrations", "warn", "no migration files found in repository")

        conn = sqlite3.connect(f"file:{paths.database_file}?mode=ro", uri=True)
        try:
            applied: dict[int, str] = {}
            try:
                cursor = conn.execute("SELECT version, checksum FROM schema_migrations")
                applied = {row[0]: row[1] for row in cursor.fetchall()}
            except sqlite3.OperationalError:
                pass

            local_versions = {m.version for m in all_migs}
            unknown_applied = sorted(set(applied) - local_versions)
            if unknown_applied:
                return DoctorCheckResult(
                    "migrations",
                    "fail",
                    "applied migration version(s) not in the local migration set: "
                    + ", ".join(str(v) for v in unknown_applied),
                    "database was migrated by a different grossy version; restore a "
                    "backup taken with the matching version",
                )

            for mig in all_migs:
                if mig.version in applied and applied[mig.version] != mig.checksum:
                    return DoctorCheckResult(
                        "migrations",
                        "fail",
                        f"checksum mismatch for migration {mig.version} ({mig.filename})",
                        "migration files may have been modified; restore from backup",
                    )

            pending = [m for m in all_migs if m.version not in applied]
            if pending:
                return DoctorCheckResult(
                    "migrations",
                    "warn",
                    f"{len(pending)} pending migration(s): "
                    + ", ".join(str(m.version) for m in pending),
                    "pending migrations will run automatically on next write operation",
                )
            return DoctorCheckResult(
                "migrations",
                "pass",
                f"all {len(all_migs)} migrations applied",
            )
        finally:
            conn.close()
    except Exception as exc:
        return DoctorCheckResult(
            "migrations",
            "fail",
            f"migration check failed: {exc}",
        )


def _check_foreign_keys(paths: GrossyPaths) -> DoctorCheckResult:
    if not paths.database_file.exists():
        return DoctorCheckResult("foreign_keys", "skipped", "database not present")
    try:
        # Open a read-only connection and apply the Grossy connection policy.
        # PRAGMA foreign_keys is a per-connection setting (not persisted in the
        # database file), so enabling it does not mutate database state.
        # Read it back to verify the policy actually takes effect.
        conn = sqlite3.connect(f"file:{paths.database_file}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            row = conn.execute("PRAGMA foreign_keys").fetchone()
            fk_enabled = row[0] if row else 0
            if fk_enabled:
                return DoctorCheckResult(
                    "foreign_keys",
                    "pass",
                    "foreign key enforcement enabled by connection policy (PRAGMA foreign_keys=ON)",
                )
            return DoctorCheckResult(
                "foreign_keys",
                "fail",
                "foreign key enforcement could not be enabled on the connection",
                "verify the database is not corrupted or locked",
            )
        finally:
            conn.close()
    except Exception as exc:
        return DoctorCheckResult(
            "foreign_keys",
            "fail",
            f"cannot check foreign keys: {exc}",
        )


def _check_wal_mode(paths: GrossyPaths) -> DoctorCheckResult:
    if not paths.database_file.exists():
        return DoctorCheckResult("wal_mode", "skipped", "database not present")
    try:
        # Read-only connection: does NOT change the journal mode.
        conn = sqlite3.connect(f"file:{paths.database_file}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            mode = row[0] if row else "unknown"
            if mode.lower() == "wal":
                return DoctorCheckResult("wal_mode", "pass", "WAL mode enabled")
            return DoctorCheckResult(
                "wal_mode",
                "fail",
                f"journal mode is {mode!r}, expected 'wal'",
                "database must be in WAL mode",
            )
        finally:
            conn.close()
    except Exception as exc:
        return DoctorCheckResult(
            "wal_mode",
            "fail",
            f"cannot check journal mode: {exc}",
        )


def _check_permissions(paths: GrossyPaths) -> DoctorCheckResult:
    issues: list[str] = []

    for name, path in [
        ("config_dir", paths.config_dir),
        ("data_dir", paths.data_dir),
        ("state_dir", paths.state_dir),
        ("backups_dir", paths.backups_dir),
    ]:
        if not path.exists():
            issues.append(f"{name} does not exist: {path}")
        else:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != DIR_MODE:
                issues.append(f"{name} has mode {oct(mode)}, expected {oct(DIR_MODE)}")

    for name, path in [
        ("config_file", paths.config_file),
        ("database_file", paths.database_file),
    ]:
        if not path.exists():
            issues.append(f"{name} does not exist: {path}")
        else:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != FILE_MODE:
                issues.append(f"{name} has mode {oct(mode)}, expected {oct(FILE_MODE)}")

    if issues:
        return DoctorCheckResult(
            "permissions",
            "fail",
            "; ".join(issues),
            "fix permissions: directories 0700, files 0600",
        )
    return DoctorCheckResult("permissions", "pass", "all permissions correct")


def _check_process_lock(paths: GrossyPaths) -> DoctorCheckResult:
    """Probe the writer lock without creating or mutating any state.

    Opens the lock file read-only (if it exists) and attempts a non-blocking
    exclusive flock. If the file does not exist, no process holds the lock.
    """
    lock_path = paths.state_dir / LOCK_FILE_NAME
    if not lock_path.exists():
        return DoctorCheckResult("process_lock", "pass", "no lock file; lock available")
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Lock acquired: no other process holds it.
            fcntl.flock(fd, fcntl.LOCK_UN)
            return DoctorCheckResult("process_lock", "pass", "lock file present but not held")
        except BlockingIOError:
            # Lock is held by another process; try to read the PID.
            holder_pid: int | None = None
            try:
                with open(lock_path) as f:
                    content = f.read().strip()
                    if content:
                        holder_pid = int(content)
            except (OSError, ValueError):
                pass
            if holder_pid is not None:
                return DoctorCheckResult(
                    "process_lock",
                    "fail",
                    f"writer lock held by PID {holder_pid}",
                    "wait for the other Grossy process to finish",
                )
            return DoctorCheckResult(
                "process_lock",
                "fail",
                "writer lock is held by another process",
                "wait for the other Grossy process to finish",
            )
        finally:
            os.close(fd)
    except OSError as exc:
        return DoctorCheckResult(
            "process_lock",
            "fail",
            f"cannot check process lock: {exc}",
        )


def _check_brave() -> DoctorCheckResult:
    for name in ("brave", "brave-browser"):
        if shutil.which(name):
            return DoctorCheckResult("brave", "pass", f"found {name!r} on PATH")
    return DoctorCheckResult(
        "brave",
        "fail",
        "Brave browser not found on PATH",
        "install Brave browser",
    )


def _check_agent_browser() -> DoctorCheckResult:
    if not shutil.which("agent-browser"):
        return DoctorCheckResult(
            "agent_browser",
            "fail",
            "agent-browser not found on PATH",
            "install agent-browser and ensure it is on PATH",
        )
    try:
        result = subprocess.run(
            ["agent-browser", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return DoctorCheckResult(
                "agent_browser",
                "fail",
                f"agent-browser --version exited with code {result.returncode}",
                "check the agent-browser installation and PATH",
            )
        # Version output is diagnostic only, not a capability or live-site check.
        version_info = sanitize_human_text(result.stdout)[:160] or "not reported"
        return DoctorCheckResult(
            "agent_browser",
            "pass",
            f"agent-browser responds (--version: {version_info}); "
            "capabilities and live compatibility not verified",
        )
    except Exception as exc:
        return DoctorCheckResult(
            "agent_browser",
            "fail",
            f"cannot probe agent-browser: {exc}",
            "check the agent-browser installation and PATH",
        )


def _check_browser_profile(paths: GrossyPaths) -> DoctorCheckResult:
    profile = paths.browser_profile_dir
    if not profile.exists():
        return DoctorCheckResult(
            "browser_profile",
            "fail",
            "browser profile directory not found",
            "run 'grossy init' to create the profile directory",
        )
    mode = stat.S_IMODE(profile.stat().st_mode)
    if mode != DIR_MODE:
        return DoctorCheckResult(
            "browser_profile",
            "fail",
            f"browser profile has mode {oct(mode)}, expected {oct(DIR_MODE)}",
            "set the configured browser profile directory to mode 0700",
        )
    return DoctorCheckResult(
        "browser_profile",
        "pass",
        "browser profile present with correct permissions",
    )


def _check_auth(browser: BrowserAdapter) -> DoctorCheckResult:
    result = browser.check_authentication()
    if result.authenticated:
        return DoctorCheckResult("auth", "pass", result.detail)
    return DoctorCheckResult(
        "auth",
        "fail",
        result.detail,
        "log in manually to AB.gr in headed Brave; Grossy never enters credentials",
    )


def _check_history_controls(browser: BrowserAdapter) -> DoctorCheckResult:
    result = browser.check_history_controls()
    if result.visible:
        return DoctorCheckResult("history_controls", "pass", result.detail)
    return DoctorCheckResult(
        "history_controls",
        "fail",
        result.detail,
        "AB.gr UI may have changed; manual review required",
    )


def _check_cart_read(browser: BrowserAdapter) -> DoctorCheckResult:
    try:
        observation = browser.read_cart()
        line_count = len(observation.lines)
        return DoctorCheckResult(
            "cart_read",
            "pass",
            f"cart read successfully ({line_count} line(s)); no mutation performed",
        )
    except Exception as exc:
        return DoctorCheckResult(
            "cart_read",
            "fail",
            f"cannot read cart: {exc}",
            "check the AB.gr cart page in headed Brave",
        )
