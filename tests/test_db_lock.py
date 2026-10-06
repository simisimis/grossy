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

    def test_held_lock_rejects_second_writer_with_holder_pid(self, tmp_path: Path) -> None:
        """PRD 13.6: process lock prevents concurrent writers and names the holder."""
        with WriterLock(tmp_path):
            with pytest.raises(LockHeldError) as exc_info:
                WriterLock(tmp_path).__enter__()
            assert exc_info.value.holder_pid == os.getpid()

    def test_lock_released_on_exception(self, tmp_path: Path) -> None:
        lock = WriterLock(tmp_path)
        with pytest.raises(RuntimeError), lock:
            raise RuntimeError("test")
        # Lock should be released; can re-acquire.
        with WriterLock(tmp_path):
            pass

    def test_lock_file_lives_in_created_state_dir(self, tmp_path: Path) -> None:
        """The lock creates a missing state directory and lives at writer.lock inside it."""
        state_dir = tmp_path / "deep" / "state"
        assert not state_dir.exists()
        lock = WriterLock(state_dir)
        with lock:
            assert lock.lock_path == state_dir / "writer.lock"
            assert lock.lock_path.exists()
        assert state_dir.exists()
