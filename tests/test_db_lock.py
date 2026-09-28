"""Tests for the Grossy process writer lock (PRD 13.6).

Covers lock acquisition, mutual exclusion, stale lock handling,
and lock file location.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from grossy.db.lock import LockHeldError, WriterLock


class TestWriterLock:
    def test_lock_acquires_and_releases(self, tmp_path: Path) -> None:
        lock = WriterLock(tmp_path)
        with lock:
            assert lock.lock_path.exists()
            # Lock file contains our PID.
            content = lock.lock_path.read_text().strip()
            assert content == str(os.getpid())
        # Lock released (file remains but lock is free).
        # Re-acquire should succeed.
        with WriterLock(tmp_path):
            pass

    def test_lock_prevents_concurrent_writers(self, tmp_path: Path) -> None:
        """PRD 13.6: process lock prevents concurrent writers."""
        lock1 = WriterLock(tmp_path)
        with lock1:
            # Second lock on same path should fail.
            lock2 = WriterLock(tmp_path)
            with pytest.raises(LockHeldError):
                lock2.__enter__()

    def test_lock_held_error_includes_pid(self, tmp_path: Path) -> None:
        lock1 = WriterLock(tmp_path)
        with lock1:
            lock2 = WriterLock(tmp_path)
            with pytest.raises(LockHeldError) as exc_info:
                lock2.__enter__()
            assert exc_info.value.holder_pid == os.getpid()

    def test_lock_released_on_exception(self, tmp_path: Path) -> None:
        lock = WriterLock(tmp_path)
        with pytest.raises(RuntimeError), lock:
            raise RuntimeError("test")
        # Lock should be released; can re-acquire.
        with WriterLock(tmp_path):
            pass

    def test_lock_file_created_in_state_dir(self, tmp_path: Path) -> None:
        lock = WriterLock(tmp_path / "state")
        with lock:
            assert lock.lock_path == tmp_path / "state" / "writer.lock"
            assert lock.lock_path.exists()

    def test_lock_creates_parent_directory(self, tmp_path: Path) -> None:
        """The lock should create the state directory if it doesn't exist."""
        state_dir = tmp_path / "deep" / "state"
        assert not state_dir.exists()
        lock = WriterLock(state_dir)
        with lock:
            pass
        assert state_dir.exists()
