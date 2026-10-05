"""Tests for the deterministic grossy doctor (PRD FR-3, FR-4, FR-5).

Verifies that doctor:
- Reports each required check
- Never mutates application or cart state
- Missing authentication yields manual-login instructions
- Fails closed on missing tools, profile, authentication, permissions
- Works fully offline with fake adapters

All tests use temporary directories and fake adapters. No live AB.gr,
no Brave, no agent-browser, no real profile.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from grossy.browser.adapter import (
    AuthCheckResult,
    FakeBrowserAdapter,
    HistoryControlsResult,
)
from grossy.doctor import (
    DOCTOR_SCHEMA_NAME,
    DOCTOR_SCHEMA_VERSION,
    DoctorResult,
    run_doctor,
)
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.init import SAMPLE_CONFIG
from grossy.paths import GrossyPaths, resolve_paths


def _make_paths(tmp_path: Path) -> GrossyPaths:
    """Create a GrossyPaths pointing at a temp directory structure."""
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    return resolve_paths(config_home=config_home, data_home=data_home, state_home=state_home)


def _init_valid_env(paths: GrossyPaths) -> None:
    """Create a valid environment for doctor tests."""
    ensure_private_dir(paths.config_dir)
    ensure_private_dir(paths.data_dir)
    ensure_private_dir(paths.backups_dir)
    ensure_private_dir(paths.state_dir)
    ensure_private_dir(paths.browser_profile_dir)
    atomic_write_text(paths.config_file, SAMPLE_CONFIG)

    # Create a minimal valid database with migrations applied.
    from grossy.db.lock import WriterLock
    from grossy.db.migrations import run_migrations

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
    paths.database_file.chmod(0o600)


@pytest.fixture
def initialized_doctor_paths(tmp_path: Path) -> GrossyPaths:
    """Build isolated valid local state for a doctor run."""
    paths = _make_paths(tmp_path)
    _init_valid_env(paths)
    return paths


def _run_doctor_with_agent_browser_result(
    paths: GrossyPaths, *, stdout: str, returncode: int = 0
) -> DoctorResult:
    """Run doctor with deterministic executable discovery/version output."""
    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], returncode, stdout=stdout),
        ),
    ):
        return run_doctor(paths)


def _check_name(result: DoctorResult, name: str) -> str:
    """Get the status of a named check from the result."""
    for c in result.checks:
        if c.name == name:
            return c.status
    raise AssertionError(f"check {name!r} not found in result")


def _check_remediation(result: DoctorResult, name: str) -> str | None:
    """Get the remediation of a named check from the result."""
    for c in result.checks:
        if c.name == name:
            return c.remediation
    raise AssertionError(f"check {name!r} not found in result")


class TestDoctorAllPass:
    """Doctor passes when all prerequisites are met."""

    def test_all_static_checks_pass(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        # Without a browser adapter, required FR-3 checks are skipped → warn
        assert result.status == "warn"
        assert not result.has_failures
        assert _check_name(result, "config") == "pass"
        assert _check_name(result, "database") == "pass"
        assert _check_name(result, "migrations") == "pass"
        assert _check_name(result, "foreign_keys") == "pass"
        assert _check_name(result, "wal_mode") == "pass"
        assert _check_name(result, "permissions") == "pass"
        assert _check_name(result, "process_lock") == "pass"
        assert _check_name(result, "brave") == "pass"
        assert _check_name(result, "agent_browser") == "pass"
        assert _check_name(result, "browser_profile") == "pass"

    def test_browser_checks_skipped_without_adapter(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "auth") == "skipped"
        assert _check_name(result, "history_controls") == "skipped"
        assert _check_name(result, "cart_read") == "skipped"
        # Skipped required checks prevent a clean "pass" status.
        assert result.status == "warn"
        assert result.has_warnings

    def test_browser_checks_pass_with_fake_adapter(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        adapter = FakeBrowserAdapter()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths, browser=adapter)
        assert _check_name(result, "auth") == "pass"
        assert _check_name(result, "history_controls") == "pass"
        assert _check_name(result, "cart_read") == "pass"
        assert result.status == "pass"
        assert not result.has_warnings


class TestDoctorConfigChecks:
    def test_missing_config_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        paths.config_file.unlink()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "config") == "fail"
        assert "init" in _check_remediation(result, "config") or _check_remediation(
            result, "config"
        )

    def test_invalid_config_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        atomic_write_text(paths.config_file, "schema_version: 999\n")
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "config") == "fail"

    def test_unknown_field_config_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        bad_config = SAMPLE_CONFIG + "\nunknown_top_level: true\n"
        atomic_write_text(paths.config_file, bad_config)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "config") == "fail"


class TestDoctorDatabaseChecks:
    def test_missing_database_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        paths.database_file.unlink()
        # Remove WAL/SHM files if present
        for suffix in ("-wal", "-shm"):
            p = paths.database_file.with_suffix(paths.database_file.suffix + suffix)
            if p.exists():
                p.unlink()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "database") == "fail"
        assert "init" in (_check_remediation(result, "database") or "")

    def test_missing_database_skips_fk_and_wal(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        paths.database_file.unlink()
        for suffix in ("-wal", "-shm"):
            p = paths.database_file.with_suffix(paths.database_file.suffix + suffix)
            if p.exists():
                p.unlink()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "foreign_keys") == "skipped"
        assert _check_name(result, "wal_mode") == "skipped"


class TestDoctorForeignKeys:
    """The FK check must verify the connection policy actually enables enforcement.

    PRD FR-3: doctor must validate foreign-key enforcement. The check must
    read PRAGMA foreign_keys back (not merely open a connection and run
    SELECT 1) to confirm the policy takes effect on the connection.
    """

    def test_fk_check_reads_pragma(self, tmp_path: Path) -> None:
        """The FK check must verify enforcement via PRAGMA foreign_keys."""
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "foreign_keys") == "pass"
        fk_check = next(c for c in result.checks if c.name == "foreign_keys")
        assert "PRAGMA foreign_keys" in fk_check.detail

    def test_fk_readback_disabled_fails(self, tmp_path: Path) -> None:
        """FK check must report failure when PRAGMA foreign_keys readback is 0.

        Simulates a corrupted or locked database where the read-only connection
        cannot enable foreign-key enforcement. The doctor must fail closed.
        """
        import sqlite3
        from unittest.mock import MagicMock

        from grossy.doctor import _check_foreign_keys

        paths = _make_paths(tmp_path)
        _init_valid_env(paths)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.execute.return_value = mock_cursor
        mock_conn.close = MagicMock()

        with patch.object(sqlite3, "connect", return_value=mock_conn):
            result = _check_foreign_keys(paths)

        assert result.status == "fail"
        assert "foreign key" in result.detail.lower()


class TestDoctorMigrationChecks:
    def test_pending_migrations_warn(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Delete the last migration from the applied state to simulate pending
        import sqlite3

        conn = sqlite3.connect(str(paths.database_file))
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = "
            "(SELECT MAX(version) FROM schema_migrations)"
        )
        conn.commit()
        conn.close()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "migrations") == "warn"


class TestDoctorNewerSchema:
    """A database at a newer or unknown schema version must fail closed."""

    def _set_applied_versions(self, paths: GrossyPaths, versions: list[int]) -> None:
        import sqlite3

        conn = sqlite3.connect(str(paths.database_file))
        try:
            conn.execute("DELETE FROM schema_migrations")
            for version in versions:
                conn.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (version, f"{version:03d}_future.sql", "0" * 64, "2026-01-01T00:00:00Z"),
                )
            conn.commit()
        finally:
            conn.close()

    def test_newer_schema_version_fails(self, tmp_path: Path) -> None:
        from grossy.db.migrations import discover_migrations

        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        supported_max = max(m.version for m in discover_migrations())
        self._set_applied_versions(paths, [supported_max + 1])
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "database") == "fail"
        assert _check_name(result, "migrations") == "fail"

    def test_unknown_applied_version_fails(self, tmp_path: Path) -> None:
        from grossy.db.migrations import discover_migrations

        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        supported_max = max(m.version for m in discover_migrations())
        # Fully applied local set plus one unknown applied version: the
        # migrations check must fail on the unknown version.
        local_versions = [m.version for m in discover_migrations()]
        self._set_applied_versions(paths, local_versions + [supported_max + 4])
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "migrations") == "fail"
        migrations_detail = next(c.detail for c in result.checks if c.name == "migrations")
        assert str(supported_max + 4) in migrations_detail

    def test_supported_schema_version_passes(self, tmp_path: Path) -> None:
        from grossy.db.migrations import discover_migrations

        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        supported_max = max(m.version for m in discover_migrations())
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "database") == "pass"
        detail = next(c.detail for c in result.checks if c.name == "database")
        assert str(supported_max) in detail


class TestDoctorPermissionChecks:
    def test_wrong_dir_permissions_fail(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Make config dir world-readable
        paths.config_dir.chmod(0o755)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "permissions") == "fail"

    def test_wrong_file_permissions_fail(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Make config file world-readable
        paths.config_file.chmod(0o644)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "permissions") == "fail"


class TestDoctorToolChecks:
    def test_missing_brave_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value=None),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "brave") == "fail"
        assert "Brave" in (_check_remediation(result, "brave") or "")

    def test_missing_agent_browser_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch(
                "shutil.which",
                side_effect=lambda x: "/usr/bin/fake" if x != "agent-browser" else None,
            ),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "agent_browser") == "fail"

    def test_wrong_agent_browser_version_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.26.0\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "agent_browser") == "fail"
        assert "0.27" in (_check_remediation(result, "agent_browser") or "")


class TestDoctorBrowserProfile:
    def test_missing_profile_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        paths.browser_profile_dir.rmdir()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "browser_profile") == "fail"
        assert "init" in (_check_remediation(result, "browser_profile") or "")

    def test_wrong_profile_permissions_fail(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        paths.browser_profile_dir.chmod(0o755)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "browser_profile") == "fail"


class TestDoctorAuthChecks:
    def test_missing_auth_yields_manual_login(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        adapter = FakeBrowserAdapter(auth=AuthCheckResult(False, "not logged in"))
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths, browser=adapter)
        assert _check_name(result, "auth") == "fail"
        remediation = _check_remediation(result, "auth")
        assert remediation is not None
        assert "manual" in remediation.lower() or "log in" in remediation.lower()


class TestDoctorHistoryControls:
    def test_history_not_visible_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        adapter = FakeBrowserAdapter(history=HistoryControlsResult(False, "order list not found"))
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths, browser=adapter)
        assert _check_name(result, "history_controls") == "fail"


class TestDoctorCartRead:
    def test_doctor_does_not_mutate_database_during_cart_read(self, tmp_path: Path) -> None:
        """A cart-read check leaves the local database file unchanged."""
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Record the database file state before doctor
        db_bytes_before = paths.database_file.read_bytes()
        adapter = FakeBrowserAdapter()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths, browser=adapter)
        db_bytes_after = paths.database_file.read_bytes()
        assert db_bytes_before == db_bytes_after
        assert _check_name(result, "cart_read") == "pass"


class TestDoctorLocalReadOnlyChecks:
    """Doctor's local checks must not modify the database or configuration."""

    def test_no_database_mutation(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        db_bytes_before = paths.database_file.read_bytes()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            run_doctor(paths)
        db_bytes_after = paths.database_file.read_bytes()
        assert db_bytes_before == db_bytes_after

    def test_no_config_mutation(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        config_before = paths.config_file.read_text()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            run_doctor(paths)
        config_after = paths.config_file.read_text()
        assert config_before == config_after


class TestDoctorOutputFormat:
    def test_result_has_correct_schema(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        d = result.to_dict()
        assert d["schema_name"] == DOCTOR_SCHEMA_NAME
        assert d["schema_version"] == DOCTOR_SCHEMA_VERSION
        assert d["status"] in ("pass", "fail", "warn")
        assert isinstance(d["checks"], list)
        assert len(d["checks"]) >= 10

    def test_each_check_has_required_fields(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        for check in result.checks:
            assert isinstance(check.name, str) and check.name
            assert check.status in ("pass", "fail", "warn", "skipped")
            assert isinstance(check.detail, str) and check.detail

    def test_all_required_check_names_present(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        names = {c.name for c in result.checks}
        required = {
            "config",
            "database",
            "migrations",
            "foreign_keys",
            "wal_mode",
            "permissions",
            "process_lock",
            "brave",
            "agent_browser",
            "browser_profile",
            "auth",
            "history_controls",
            "cart_read",
        }
        assert required == names


class TestDoctorLockHeld:
    def test_lock_held_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Hold the lock in a child process
        from grossy.db.lock import WriterLock

        lock = WriterLock(paths.state_dir)
        lock.__enter__()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        lock.__exit__(None, None, None)
        assert _check_name(result, "process_lock") == "fail"


class TestDoctorNonMutatingWAL:
    """Doctor WAL check must not mutate the database journal mode."""

    def test_non_wal_database_stays_non_wal(self, tmp_path: Path) -> None:
        """A non-WAL database must not be changed to WAL by the doctor."""
        import sqlite3

        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Force the database into DELETE journal mode (non-WAL)
        conn = sqlite3.connect(str(paths.database_file))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        # Verify it's actually DELETE mode
        conn = sqlite3.connect(str(paths.database_file))
        mode_before = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode_before.lower() == "delete"

        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)

        # Doctor must report the WAL check as failing
        assert _check_name(result, "wal_mode") == "fail"
        # Database must still be in DELETE mode (not mutated by doctor)
        conn = sqlite3.connect(str(paths.database_file))
        mode_after = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode_after.lower() == "delete"


class TestDoctorNonMutatingLock:
    """Doctor lock check must not create state directory or lock file."""

    def test_no_state_dir_created(self, tmp_path: Path) -> None:
        """Doctor must not create the state directory if it doesn't exist."""
        paths = _make_paths(tmp_path)
        # Create a valid env but remove the state directory
        _init_valid_env(paths)
        import shutil as _shutil

        _shutil.rmtree(paths.state_dir)

        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)

        # The state directory must NOT have been created by the doctor
        assert not paths.state_dir.exists()
        # Lock check should still pass (no lock file means lock is available)
        assert _check_name(result, "process_lock") == "pass"

    def test_no_lock_file_created(self, tmp_path: Path) -> None:
        """Doctor must not create a lock file if one doesn't exist."""
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Remove the lock file but keep the state directory
        from grossy.db.lock import LOCK_FILE_NAME

        lock_path = paths.state_dir / LOCK_FILE_NAME
        if lock_path.exists():
            lock_path.unlink()

        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)

        # Lock file must not have been created
        assert not lock_path.exists()
        assert _check_name(result, "process_lock") == "pass"


class TestDoctorBackupsDirPermission:
    """Doctor must check the backups_dir permission (PRD §13)."""

    def test_wrong_backups_dir_permissions_fail(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        paths.backups_dir.chmod(0o755)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "permissions") == "fail"

    def test_missing_backups_dir_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        import shutil as _shutil

        _shutil.rmtree(paths.backups_dir)
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths)
        assert _check_name(result, "permissions") == "fail"


class TestDoctorAgentBrowserVersion:
    """Named compatibility and failure cases for the agent-browser version gate."""

    @pytest.mark.parametrize(
        ("stdout", "returncode", "expected_status", "remediation"),
        [
            pytest.param("0.27.4\n", 0, "pass", None, id="supported-0.27-patch"),
            pytest.param("agent-browser 0.38.1\n", 0, "pass", None, id="installed-0.38.1"),
            pytest.param("0.38.1\n", 0, "pass", None, id="bare-0.38.1"),
            pytest.param("agent-browser 0.39.0\n", 0, "fail", "0.38", id="future-minor"),
            pytest.param("agent-browser 1.38.1\n", 0, "fail", None, id="future-major"),
            pytest.param("other-tool 0.38.1\n", 0, "fail", None, id="wrong-prefix"),
            pytest.param("agent-browser 0.38.1junk\n", 0, "fail", None, id="prefixed-junk"),
            pytest.param("agent-browser 0.38.1\n", 1, "fail", None, id="prefixed-command-failed"),
            pytest.param("0.26.0\n", 0, "fail", "0.27", id="unsupported-minor"),
            pytest.param("0.28.0\n", 0, "fail", None, id="unsupported-next-minor"),
            pytest.param("0.27.not-a-version\n", 0, "fail", None, id="malformed-version"),
            pytest.param("0.27.1junk\n", 0, "fail", None, id="trailing-junk"),
            pytest.param("0.27.1\n", 1, "fail", None, id="command-failed"),
        ],
    )
    def test_version_gate(
        self,
        initialized_doctor_paths: GrossyPaths,
        stdout: str,
        returncode: int,
        expected_status: str,
        remediation: str | None,
    ) -> None:
        result = _run_doctor_with_agent_browser_result(
            initialized_doctor_paths, stdout=stdout, returncode=returncode
        )
        assert _check_name(result, "agent_browser") == expected_status
        if remediation is not None:
            assert remediation in (_check_remediation(result, "agent_browser") or "")


class TestDoctorWarnAggregation:
    """A 'warn' check must aggregate into an overall 'warn' status.

    The JSON status and the CLI exit code (2 for warnings) must agree.
    """

    def test_warn_check_aggregates_to_overall_warn(self, tmp_path: Path) -> None:
        import sqlite3

        paths = _make_paths(tmp_path)
        _init_valid_env(paths)
        # Simulate a pending migration so the migrations check warns while a
        # browser adapter is present.
        conn = sqlite3.connect(str(paths.database_file))
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = "
            "(SELECT MAX(version) FROM schema_migrations)"
        )
        conn.commit()
        conn.close()
        adapter = FakeBrowserAdapter()
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0.27.1\n"),
            ),
        ):
            result = run_doctor(paths, browser=adapter)
        assert _check_name(result, "migrations") == "warn"
        assert result.status == "warn"
        assert result.has_warnings
