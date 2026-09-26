"""Process writer lock for Grossy SQLite (PRD 13.6, 12.1).

Prevents concurrent writers using a file-based exclusive lock under
Grossy's local state directory. Uses ``fcntl.flock`` for robustness:
the kernel releases the lock automatically if the process dies.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_FILE_NAME = "writer.lock"


class LockHeldError(Exception):
    """Raised when the writer lock is already held by another process."""

    def __init__(self, lock_path: Path, holder_pid: int | None = None) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        if holder_pid is not None:
            super().__init__(f"writer lock held by PID {holder_pid} at {lock_path}")
        else:
            super().__init__(f"writer lock held at {lock_path}")


class WriterLock:
    """Context manager for the Grossy single-writer process lock.

    Usage::

        with WriterLock(state_dir):
            # perform SQLite writes
            ...
    """

    def __init__(self, state_dir: Path) -> None:
        self._lock_path = state_dir / LOCK_FILE_NAME
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def __enter__(self) -> WriterLock:
        # Ensure the state directory exists.
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            # Try to read the PID for diagnostics.
            holder_pid: int | None = None
            try:
                with open(self._lock_path) as f:
                    content = f.read().strip()
                    if content:
                        holder_pid = int(content)
            except (OSError, ValueError):
                pass
            raise LockHeldError(self._lock_path, holder_pid) from None
        self._fd = fd
        # Record our PID for diagnostics.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
