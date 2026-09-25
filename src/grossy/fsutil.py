"""Filesystem helpers with the Grossy permission policy (PRD section 13).

Directories use mode ``0700`` and data files use mode ``0600``. Structured
artifacts are written with atomic replacement: a temporary file in the same
directory is fully written and fsynced before being renamed over the target.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def ensure_private_dir(path: str | Path) -> Path:
    """Create ``path`` and any missing parents under the ``0700`` policy.

    New directories are created with mode ``0700`` at ``mkdir`` time, so
    privacy does not depend on the follow-up ``chmod`` completing (the
    ``chmod`` only guards against a umask clearing owner bits). Existing
    ancestor directories (for example a shared XDG home) are never touched;
    an existing target directory is brought onto the ``0700`` policy rather
    than left group- or world-readable. A regular file at ``path`` is an
    error.
    """
    directory = Path(path)
    to_create: list[Path] = []
    current = directory
    while not current.exists():
        to_create.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for created in reversed(to_create):
        created.mkdir(mode=DIR_MODE)
        created.chmod(DIR_MODE)
    if directory.is_symlink() or not directory.is_dir():
        raise FileExistsError(f"not a directory: {directory}")
    if stat.S_IMODE(directory.stat().st_mode) != DIR_MODE:
        directory.chmod(DIR_MODE)
    return directory


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: str | Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data`` under the ``0600`` file policy.

    The parent directory must already exist. The replacement is a same-directory
    rename, so readers observe either the old or the new content.
    """
    target = Path(path)
    parent = target.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"parent directory missing for atomic write: {parent}")
    tmp = parent / f".{target.name}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(parent)


def atomic_write_text(path: str | Path, text: str) -> None:
    """Atomically write UTF-8 text under the ``0600`` file policy."""
    atomic_write(path, text.encode("utf-8"))


def enforce_file_mode(path: str | Path) -> None:
    """Bring an existing data file onto the ``0600`` policy."""
    Path(path).chmod(FILE_MODE)


def enforce_dir_mode(path: str | Path) -> None:
    """Bring an existing directory onto the ``0700`` policy."""
    Path(path).chmod(DIR_MODE)
