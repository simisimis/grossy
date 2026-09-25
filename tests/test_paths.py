"""Tests for XDG path resolution (PRD section 13).

All tests operate on temporary or fake homes; no real user directory is
read or created.
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from grossy.paths import GrossyPaths, resolve_paths


def _fake_expanduser(home: Path) -> Any:
    def expanduser(value: str) -> str:
        return value.replace("~", str(home), 1)

    return expanduser


def test_explicit_homes(tmp_path: Path) -> None:
    paths = resolve_paths(
        config_home=tmp_path / "config",
        data_home=tmp_path / "data",
        state_home=tmp_path / "state",
    )
    assert paths.config_file == tmp_path / "config" / "grossy" / "config.yaml"
    assert paths.database_file == tmp_path / "data" / "grossy" / "grossy.sqlite3"
    assert paths.backups_dir == tmp_path / "data" / "grossy" / "backups"
    assert (
        paths.browser_profile_dir == tmp_path / "data" / "grossy" / "agent-browser" / "ab-profile"
    )
    assert paths.runs_dir == tmp_path / "state" / "grossy" / "runs"


def test_xdg_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "dat"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "sta"))
    paths = resolve_paths()
    assert paths.config_dir == tmp_path / "cfg" / "grossy"
    assert paths.data_dir == tmp_path / "dat" / "grossy"
    assert paths.state_dir == tmp_path / "sta" / "grossy"


def test_explicit_wins_over_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    paths = resolve_paths(config_home=tmp_path / "explicit")
    assert paths.config_dir == tmp_path / "explicit" / "grossy"


def test_defaults_follow_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", _fake_expanduser(tmp_path))
    paths = resolve_paths()
    assert paths.config_dir == tmp_path / ".config" / "grossy"
    assert paths.data_dir == tmp_path / ".local" / "share" / "grossy"
    assert paths.state_dir == tmp_path / ".local" / "state" / "grossy"


def test_paths_are_frozen(tmp_path: Path) -> None:
    paths = resolve_paths(
        config_home=tmp_path / "c",
        data_home=tmp_path / "d",
        state_home=tmp_path / "s",
    )
    assert isinstance(paths, GrossyPaths)
    with pytest.raises(FrozenInstanceError):
        paths.config_dir = Path("x")  # type: ignore[misc]


@pytest.mark.parametrize(
    "run_id",
    ["", ".", "..", "a/b", "a\\b"],
)
def test_run_dir_rejects_unsafe_ids(run_id: str, tmp_path: Path) -> None:
    paths = resolve_paths(state_home=tmp_path / "state")
    with pytest.raises(ValueError):
        paths.run_dir(run_id)


def test_relative_xdg_environment_value_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Regression: a relative XDG value would redirect user state into the
    # current directory (possibly the repository); it must be ignored in
    # favor of the standard default.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", _fake_expanduser(tmp_path))
    paths = resolve_paths()
    assert paths.data_dir == tmp_path / ".local" / "share" / "grossy"
    assert paths.database_file == tmp_path / ".local" / "share" / "grossy" / "grossy.sqlite3"


def test_empty_xdg_environment_value_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", _fake_expanduser(tmp_path))
    paths = resolve_paths()
    assert paths.config_dir == tmp_path / ".config" / "grossy"


def test_relative_explicit_base_is_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_paths(data_home="relative-data")


def test_run_dir(tmp_path: Path) -> None:
    paths = resolve_paths(state_home=tmp_path / "state")
    assert paths.run_dir("run-1") == tmp_path / "state" / "grossy" / "runs" / "run-1"
