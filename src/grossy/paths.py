"""Canonical XDG path resolution for Grossy (PRD section 13).

Resolves paths only; never creates directories. Creation with the 0700
mode policy lives in :mod:`grossy.fsutil`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "grossy"
CONFIG_FILE_NAME = "config.yaml"
DATABASE_FILE_NAME = "grossy.sqlite3"


@dataclass(frozen=True)
class GrossyPaths:
    """Canonical Grossy locations derived from the XDG base directories."""

    config_dir: Path
    data_dir: Path
    state_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILE_NAME

    @property
    def database_file(self) -> Path:
        return self.data_dir / DATABASE_FILE_NAME

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "agent-browser" / "ab-profile"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
            raise ValueError(f"invalid run ID: {run_id!r}")
        return self.runs_dir / run_id


def _base(
    explicit: str | Path | None,
    env_name: str,
    default: Path,
) -> Path:
    """Resolve one XDG base directory.

    Explicit bases must be absolute: a relative explicit base would place
    user data inside the current working directory, which may be the
    repository (PRD section 13 requires user data outside the repository).
    Relative XDG environment values are invalid for the same reason and are
    ignored in favor of the standard default.
    """
    if explicit is not None:
        base = Path(explicit)
        if not base.is_absolute():
            raise ValueError(
                f"explicit base for {env_name} must be an absolute path, got {explicit!r}"
            )
        return base
    value = os.environ.get(env_name)
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
    return default


def resolve_paths(
    config_home: str | Path | None = None,
    data_home: str | Path | None = None,
    state_home: str | Path | None = None,
) -> GrossyPaths:
    """Resolve Grossy paths from explicit homes, XDG environment, or defaults.

    Explicit arguments win over the XDG environment, which wins over the
    standard ``~/.config``, ``~/.local/share``, and ``~/.local/state``
    defaults. Explicit bases must be absolute (relative bases are a
    ``ValueError``); relative XDG environment values are ignored and the
    defaults are used.
    """
    home = Path.home()
    return GrossyPaths(
        config_dir=_base(config_home, "XDG_CONFIG_HOME", home / ".config") / APP_NAME,
        data_dir=_base(data_home, "XDG_DATA_HOME", home / ".local" / "share") / APP_NAME,
        state_dir=_base(state_home, "XDG_STATE_HOME", home / ".local" / "state") / APP_NAME,
    )
