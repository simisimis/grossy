"""Tests for atomic replacement and the 0700/0600 permission policy.

Every test runs inside a temporary directory; the real user XDG
directories are never touched (PRD section 13, task 01 scope).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from grossy.fsutil import (
    DIR_MODE,
    FILE_MODE,
    atomic_write,
    atomic_write_text,
    enforce_dir_mode,
    enforce_file_mode,
    ensure_private_dir,
)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestEnsurePrivateDir:
    def test_creates_nested_directories_with_0700(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        result = ensure_private_dir(target)
        assert result == target
        for directory in (tmp_path / "a", tmp_path / "a" / "b", target):
            assert mode_of(directory) == DIR_MODE

    def test_existing_world_readable_target_is_brought_private(self, tmp_path: Path) -> None:
        # Regression: an existing 0755 target must be brought onto the 0700
        # policy, not returned unchanged and implied private.
        existing = tmp_path / "existing"
        existing.mkdir(mode=0o755)
        assert ensure_private_dir(existing) == existing
        assert mode_of(existing) == DIR_MODE

    def test_new_directories_are_private_from_mkdir_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: mkdir must be called with the 0700 mode so privacy does
        # not depend on the follow-up chmod completing (under a permissive
        # umask a plain mkdir() is briefly world/group-accessible).
        observed_modes: list[int] = []
        real_mkdir = Path.mkdir

        def spying_mkdir(
            self: Path, mode: int = 0o777, *, parents: bool = False, exist_ok: bool = False
        ) -> None:
            observed_modes.append(mode)
            real_mkdir(self, mode, parents=parents, exist_ok=exist_ok)

        monkeypatch.setattr(Path, "mkdir", spying_mkdir)
        previous_umask = os.umask(0)
        try:
            ensure_private_dir(tmp_path / "a" / "b" / "c")
        finally:
            os.umask(previous_umask)
        assert observed_modes == [DIR_MODE, DIR_MODE, DIR_MODE]
        assert mode_of(tmp_path / "a" / "b" / "c") == DIR_MODE

    def test_existing_ancestors_are_not_chmodded(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755)
        ensure_private_dir(shared / "grossy")
        assert mode_of(shared) == 0o755
        assert mode_of(shared / "grossy") == DIR_MODE

    def test_file_at_target_raises(self, tmp_path: Path) -> None:
        file = tmp_path / "file"
        file.write_text("x")
        with pytest.raises(FileExistsError):
            ensure_private_dir(file)

    def test_enforce_dir_mode(self, tmp_path: Path) -> None:
        directory = tmp_path / "d"
        directory.mkdir(mode=0o755)
        enforce_dir_mode(directory)
        assert mode_of(directory) == DIR_MODE


class TestAtomicWrite:
    def test_writes_content_with_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        atomic_write(target, b'{"ok": true}')
        assert target.read_bytes() == b'{"ok": true}'
        assert mode_of(target) == FILE_MODE

    def test_no_temp_file_left_behind(self, tmp_path: Path) -> None:
        atomic_write(tmp_path / "f", b"data")
        leftovers = [entry for entry in tmp_path.iterdir() if entry.name.startswith(".")]
        assert leftovers == []

    def test_replaces_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "f"
        target.write_text("old")
        atomic_write(target, b"new")
        assert target.read_bytes() == b"new"
        assert mode_of(target) == FILE_MODE

    def test_replacement_is_visible_as_single_content(self, tmp_path: Path) -> None:
        target = tmp_path / "f"
        first = b"x" * 100
        atomic_write(target, first)
        atomic_write(target, b"y")
        assert target.read_bytes() == b"y"

    def test_missing_parent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            atomic_write(tmp_path / "missing" / "f", b"data")

    def test_text_is_utf8(self, tmp_path: Path) -> None:
        target = tmp_path / "f"
        atomic_write_text(target, "π and €")
        assert target.read_text(encoding="utf-8") == "π and €"
        assert mode_of(target) == FILE_MODE

    def test_failure_leaves_old_content_and_no_tmp(self, tmp_path: Path) -> None:
        parent = tmp_path / "locked"
        parent.mkdir()
        target = parent / "f"
        atomic_write(target, b"old")
        parent.chmod(0o500)
        try:
            with pytest.raises(OSError):
                atomic_write(target, b"new")
        finally:
            parent.chmod(0o700)
        assert target.read_bytes() == b"old"
        leftovers = [entry for entry in parent.iterdir() if entry.name.startswith(".")]
        assert leftovers == []


class TestEnforceFileMode:
    def test_brings_file_onto_policy(self, tmp_path: Path) -> None:
        target = tmp_path / "f"
        target.write_text("x")
        target.chmod(0o644)
        enforce_file_mode(target)
        assert mode_of(target) == FILE_MODE
