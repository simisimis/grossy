"""Tests for the deterministic grossy doctor (PRD FR-3, FR-4, FR-5).

Verifies that doctor:
- Reports each required check
- Never mutates application or cart state
- Missing authentication yields manual-login instructions
- Fails closed on missing tools, profile, authentication, permissions
- Works fully offline with fake adapters

All tests use temporary directories and fake adapters. No live store,
no Brave, no agent-browser, no real profile.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from grossy.browser.adapter import (
    AuthCheckResult,
    BrowserAdapter,
    FakeBrowserAdapter,
    HistoryControlsResult,
)
from grossy.db.lock import LOCK_FILE_NAME, WriterLock
from grossy.db.migrations import discover_migrations, run_migrations
from grossy.doctor import (
    DOCTOR_SCHEMA_NAME,
    DOCTOR_SCHEMA_VERSION,
    DoctorCheckResult,
    DoctorResult,
    run_doctor,
)
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.init import SAMPLE_CONFIG
from grossy.paths import GrossyPaths, resolve_paths
from grossy.store import STORE_URL_ENV

SUPPORTED_MAX = max(m.version for m in discover_migrations())


def _make_paths(tmp_path: Path) -> GrossyPaths:
    """Create a GrossyPaths pointing at a temp directory structure."""
    return resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )


def _init_valid_env(paths: GrossyPaths) -> None:
    """Create a valid environment for doctor tests."""
    ensure_private_dir(paths.config_dir)
    ensure_private_dir(paths.data_dir)
    ensure_private_dir(paths.backups_dir)
    ensure_private_dir(paths.state_dir)
    ensure_private_dir(paths.browser_profile_dir)
    atomic_write_text(paths.config_file, SAMPLE_CONFIG)
    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
    paths.database_file.chmod(0o600)


@pytest.fixture
def paths(tmp_path: Path) -> GrossyPaths:
    """Isolated valid local state for a doctor run."""
    built = _make_paths(tmp_path)
    _init_valid_env(built)
    return built


def _doctor(
    paths: GrossyPaths,
    *,
    browser: BrowserAdapter | None = None,
    which: Callable[[str], str | None] | None = None,
    stdout: str = "agent-browser development build\n",
    returncode: int = 0,
) -> DoctorResult:
    """Run doctor with deterministic executable discovery and probe output."""
    with (
        patch("shutil.which", side_effect=which or (lambda _: "/usr/bin/fake")),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], returncode, stdout=stdout),
        ),
    ):
        return run_doctor(paths, browser=browser)


def _check(result: DoctorResult, name: str) -> DoctorCheckResult:
    for c in result.checks:
        if c.name == name:
            return c
    raise AssertionError(f"check {name!r} not found in result")


def _check_name(result: DoctorResult, name: str) -> str:
    """Get the status of a named check from the result."""
    return _check(result, name).status


def _check_remediation(result: DoctorResult, name: str) -> str:
    """Get the remediation of a named check from the result ("" when absent)."""
    return _check(result, name).remediation or ""


def _check_detail(result: DoctorResult, name: str) -> str:
    return _check(result, name).detail


def _set_applied_versions(paths: GrossyPaths, versions: list[int]) -> None:
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


def _delete_latest_migration_row(paths: GrossyPaths) -> None:
    conn = sqlite3.connect(str(paths.database_file))
    conn.execute(
        "DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)"
    )
    conn.commit()
    conn.close()


def _remove_database(paths: GrossyPaths) -> None:
    paths.database_file.unlink()
    for suffix in ("-wal", "-shm"):
        p = paths.database_file.with_suffix(paths.database_file.suffix + suffix)
        if p.exists():
            p.unlink()


class TestDoctorAllPass:
    """Doctor passes when all prerequisites are met and never mutates local state."""

    def test_static_checks_pass_and_browser_checks_skip_without_adapter(
        self, paths: GrossyPaths
    ) -> None:
        db_before = paths.database_file.read_bytes()
        config_before = paths.config_file.read_text()
        result = _doctor(paths)
        for name in (
            "store_url",
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
        ):
            assert _check_name(result, name) == "pass", name
        assert str(SUPPORTED_MAX) in _check_detail(result, "database")
        # PRD FR-3: the FK check reads the pragma back rather than merely connecting.
        assert "PRAGMA foreign_keys" in _check_detail(result, "foreign_keys")
        # Without a browser adapter, required FR-3 checks are skipped -> warn, never pass.
        for name in ("auth", "history_controls", "cart_read"):
            assert _check_name(result, name) == "skipped", name
        assert result.status == "warn"
        assert result.has_warnings and not result.has_failures
        assert paths.database_file.read_bytes() == db_before
        assert paths.config_file.read_text() == config_before

    def test_browser_checks_pass_with_fake_adapter_without_database_mutation(
        self, paths: GrossyPaths
    ) -> None:
        db_before = paths.database_file.read_bytes()
        result = _doctor(paths, browser=FakeBrowserAdapter())
        for name in ("auth", "history_controls", "cart_read"):
            assert _check_name(result, name) == "pass", name
        assert result.status == "pass"
        assert not result.has_warnings
        assert paths.database_file.read_bytes() == db_before

    def test_result_schema_and_required_check_names(self, paths: GrossyPaths) -> None:
        result = _doctor(paths)
        d = result.to_dict()
        assert d["schema_name"] == DOCTOR_SCHEMA_NAME
        assert d["schema_version"] == DOCTOR_SCHEMA_VERSION
        assert d["status"] in ("pass", "fail", "warn")
        assert {c.name for c in result.checks} == {
            "store_url",
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
        for check in result.checks:
            assert check.status in ("pass", "fail", "warn", "skipped")
            assert isinstance(check.detail, str) and check.detail


@pytest.mark.parametrize(
    "break_env,check,remediation",
    [
        pytest.param(lambda p: p.config_file.unlink(), "config", "init", id="missing-config"),
        pytest.param(
            lambda p: atomic_write_text(p.config_file, "schema_version: 999\n"),
            "config",
            "",
            id="invalid-config",
        ),
        pytest.param(
            lambda p: atomic_write_text(
                p.config_file, SAMPLE_CONFIG + "\nunknown_top_level: true\n"
            ),
            "config",
            "",
            id="unknown-config-field",
        ),
        pytest.param(
            lambda p: p.config_dir.chmod(0o755), "permissions", "", id="world-readable-dir"
        ),
        pytest.param(
            lambda p: p.config_file.chmod(0o644), "permissions", "", id="world-readable-file"
        ),
        pytest.param(
            lambda p: p.backups_dir.chmod(0o755), "permissions", "", id="world-readable-backups"
        ),
        pytest.param(
            lambda p: shutil.rmtree(p.backups_dir), "permissions", "", id="missing-backups-dir"
        ),
        pytest.param(
            lambda p: p.browser_profile_dir.rmdir(), "browser_profile", "init", id="missing-profile"
        ),
        pytest.param(
            lambda p: p.browser_profile_dir.chmod(0o755),
            "browser_profile",
            "",
            id="world-readable-profile",
        ),
    ],
)
def test_local_check_fails_closed(
    paths: GrossyPaths, break_env: Callable[[GrossyPaths], object], check: str, remediation: str
) -> None:
    break_env(paths)
    result = _doctor(paths)
    assert result.status == "fail"
    assert _check_name(result, check) == "fail"
    assert remediation in _check_remediation(result, check)


class TestDoctorStoreUrlCheck:
    """The static ``store_url`` check validates ``GROSSY_STORE_URL`` and fails closed."""

    def test_valid_setting_passes(self, paths: GrossyPaths) -> None:
        result = _doctor(paths)
        assert _check_name(result, "store_url") == "pass"
        assert STORE_URL_ENV in _check_detail(result, "store_url")

    def test_malformed_setting_fails_with_reason(
        self, paths: GrossyPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STORE_URL_ENV, "https://store.example/")
        result = _doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "store_url") == "fail"
        detail = _check_detail(result, "store_url")
        assert STORE_URL_ENV in detail and "https://HOST" in detail
        assert STORE_URL_ENV in _check_remediation(result, "store_url")


class TestDoctorDatabaseChecks:
    def test_missing_database_fails_and_skips_dependent_checks(self, paths: GrossyPaths) -> None:
        _remove_database(paths)
        result = _doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "database") == "fail"
        assert "init" in _check_remediation(result, "database")
        assert _check_name(result, "foreign_keys") == "skipped"
        assert _check_name(result, "wal_mode") == "skipped"

    def test_fk_readback_disabled_fails(self, paths: GrossyPaths) -> None:
        """A read-only connection that cannot enable FK enforcement fails closed."""
        from grossy.doctor import _check_foreign_keys

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.execute.return_value = mock_cursor
        with patch.object(sqlite3, "connect", return_value=mock_conn):
            result = _check_foreign_keys(paths)
        assert result.status == "fail"
        assert "foreign key" in result.detail.lower()

    def test_non_wal_database_fails_and_stays_non_wal(self, paths: GrossyPaths) -> None:
        """The WAL check reports a DELETE-journal database without converting it."""
        conn = sqlite3.connect(str(paths.database_file))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        result = _doctor(paths)
        assert _check_name(result, "wal_mode") == "fail"
        conn = sqlite3.connect(str(paths.database_file))
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        conn.close()

    def test_newer_schema_version_fails_closed(self, paths: GrossyPaths) -> None:
        _set_applied_versions(paths, [SUPPORTED_MAX + 1])
        result = _doctor(paths)
        assert result.status == "fail"
        assert _check_name(result, "database") == "fail"
        assert _check_name(result, "migrations") == "fail"

    def test_unknown_applied_version_fails_migrations_check(self, paths: GrossyPaths) -> None:
        # Fully applied local set plus one unknown applied version.
        local_versions = [m.version for m in discover_migrations()]
        _set_applied_versions(paths, local_versions + [SUPPORTED_MAX + 4])
        result = _doctor(paths)
        assert _check_name(result, "migrations") == "fail"
        assert str(SUPPORTED_MAX + 4) in _check_detail(result, "migrations")

    def test_pending_migration_warns_and_aggregates_to_overall_warn(
        self, paths: GrossyPaths
    ) -> None:
        """A 'warn' check aggregates into 'warn' even when every other check passes."""
        _delete_latest_migration_row(paths)
        result = _doctor(paths, browser=FakeBrowserAdapter())
        assert _check_name(result, "migrations") == "warn"
        assert result.status == "warn"
        assert result.has_warnings


class TestDoctorToolChecks:
    def test_missing_brave_fails(self, paths: GrossyPaths) -> None:
        result = _doctor(paths, which=lambda _: None)
        assert _check_name(result, "brave") == "fail"
        assert "Brave" in _check_remediation(result, "brave")

    def test_missing_agent_browser_fails(self, paths: GrossyPaths) -> None:
        result = _doctor(paths, which=lambda x: "/usr/bin/fake" if x != "agent-browser" else None)
        assert _check_name(result, "agent_browser") == "fail"

    def test_agent_browser_version_output_is_informational(self, paths: GrossyPaths) -> None:
        result = _doctor(paths, stdout="agent-browser development build\n")
        assert _check_name(result, "agent_browser") == "pass"
        detail = _check_detail(result, "agent_browser")
        assert "agent-browser development build" in detail
        assert "capabilities and live compatibility not verified" in detail

    def test_agent_browser_empty_version_output_is_not_a_gate(self, paths: GrossyPaths) -> None:
        result = _doctor(paths, stdout="")
        assert _check_name(result, "agent_browser") == "pass"
        assert "not reported" in _check_detail(result, "agent_browser")

    def test_agent_browser_diagnostic_output_is_sanitized_and_bounded(
        self, paths: GrossyPaths
    ) -> None:
        result = _doctor(paths, stdout="<script>\n" + "x" * 1000)
        detail = _check_detail(result, "agent_browser")
        assert _check_name(result, "agent_browser") == "pass"
        assert "<script>" not in detail
        assert "\n" not in detail
        assert "x" * 161 not in detail

    def test_agent_browser_failed_probe_fails(self, paths: GrossyPaths) -> None:
        result = _doctor(paths, stdout="agent-browser development build\n", returncode=1)
        assert _check_name(result, "agent_browser") == "fail"
        assert "exited with code 1" in _check_detail(result, "agent_browser")
        assert "installation and PATH" in _check_remediation(result, "agent_browser")

    @pytest.mark.parametrize(
        "error",
        [OSError("cannot execute"), subprocess.TimeoutExpired(["agent-browser", "--version"], 10)],
        ids=["os-error", "timeout"],
    )
    def test_agent_browser_probe_errors_fail(self, paths: GrossyPaths, error: Exception) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch("subprocess.run", side_effect=error) as probe,
        ):
            result = run_doctor(paths)
        assert _check_name(result, "agent_browser") == "fail"
        assert "cannot probe agent-browser" in _check_detail(result, "agent_browser")
        probe.assert_called_once_with(
            ["agent-browser", "--version"], capture_output=True, text=True, timeout=10
        )


class TestDoctorBrowserChecks:
    def test_missing_auth_yields_manual_login(self, paths: GrossyPaths) -> None:
        adapter = FakeBrowserAdapter(auth=AuthCheckResult(False, "not logged in"))
        result = _doctor(paths, browser=adapter)
        assert _check_name(result, "auth") == "fail"
        remediation = _check_remediation(result, "auth").lower()
        assert "manual" in remediation or "log in" in remediation

    def test_history_not_visible_fails(self, paths: GrossyPaths) -> None:
        adapter = FakeBrowserAdapter(history=HistoryControlsResult(False, "order list not found"))
        result = _doctor(paths, browser=adapter)
        assert _check_name(result, "history_controls") == "fail"


class TestDoctorLockChecks:
    """The lock check reports a held lock and never creates state or lock files."""

    def test_lock_held_fails(self, paths: GrossyPaths) -> None:
        with WriterLock(paths.state_dir):
            result = _doctor(paths)
        assert _check_name(result, "process_lock") == "fail"

    def test_no_state_dir_created(self, paths: GrossyPaths) -> None:
        shutil.rmtree(paths.state_dir)
        result = _doctor(paths)
        assert not paths.state_dir.exists()
        assert _check_name(result, "process_lock") == "pass"

    def test_no_lock_file_created(self, paths: GrossyPaths) -> None:
        lock_path = paths.state_dir / LOCK_FILE_NAME
        lock_path.unlink(missing_ok=True)
        result = _doctor(paths)
        assert not lock_path.exists()
        assert _check_name(result, "process_lock") == "pass"
