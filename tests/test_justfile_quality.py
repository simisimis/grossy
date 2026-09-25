"""Regression tests for the offline/locked Justfile quality recipes (PRD 16.2, 22.14).

Copies the repository Justfile into an isolated temporary project and runs
the real recipes against a controlled fake ``uv`` executable on PATH that
records every invocation. This proves:

- ``check`` first runs ``uv lock --check --offline`` (stale-lock
  validation) and stops before any quality step when the lock check fails;
- every quality step runs with ``--offline`` and ``--frozen`` (no network,
  no manifest drift);
- ``setup`` stays locked (``uv sync --locked``).

The real project manifests are only read, never touched; the fake tool never
accesses the network or live services. Skipped when ``just`` is not on PATH
(outside the Nix development shell).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None,
    reason="just only available inside the Nix development shell",
)

FAKE_UV_SCRIPT = r"""#!/bin/sh
{
    printf 'UV'
    for arg in "$@"; do
        printf '\t%s' "$arg"
    done
    printf '\n'
} >> "$FAKE_UV_LOG"
if [ -n "$FAKE_UV_STALE_LOCK" ] && [ "$1" = "lock" ] && [ "$2" = "--check" ]; then
    exit 1
fi
exit 0
"""


def _install_fake_uv(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-args.log"
    script = bin_dir / "uv"
    script.write_text(FAKE_UV_SCRIPT)
    script.chmod(stat.S_IRWXU)
    return log


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "Justfile").write_text((REPO_ROOT / "Justfile").read_text(encoding="utf-8"))
    # Decoy manifest: the isolated project must not be confused with the
    # real repository, and the fake uv never reads or writes it.
    (project / "pyproject.toml").write_text('name = "decoy"\nversion = "0.0.0"\n')
    return project


def _run_just(
    project: Path, tmp_path: Path, *args: str, stale_lock: bool = False
) -> subprocess.CompletedProcess[str]:
    log = _install_fake_uv(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_UV_LOG"] = str(log)
    env["FAKE_UV_STALE_LOCK"] = "1" if stale_lock else ""
    return subprocess.run(
        ["just", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _uv_invocations(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    invocations = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "UV":
            invocations.append([])
        elif line.startswith("UV\t"):
            invocations.append(line[3:].split("\t"))
    return invocations


def test_check_validates_lock_first_and_runs_quality_offline_frozen(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "check")
    assert result.returncode == 0, result.stderr
    invocations = _uv_invocations(tmp_path / "uv-args.log")
    # Stale-lock validation runs before any quality step.
    assert invocations[0] == ["lock", "--check", "--offline"]
    quality = invocations[1:]
    assert [invocation[3:] for invocation in quality] == [
        ["ruff", "format", "--check", "src", "tests"],
        ["ruff", "check", "src", "tests"],
        ["mypy"],
        ["pytest"],
    ]
    # Every quality run is offline (no network) and frozen (locked manifest).
    for invocation in quality:
        assert invocation[:3] == ["run", "--offline", "--frozen"]


def test_stale_lock_stops_check_before_quality_steps(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "check", stale_lock=True)
    assert result.returncode != 0
    # The lock check failed; no quality step may run after it.
    assert _uv_invocations(tmp_path / "uv-args.log") == [["lock", "--check", "--offline"]]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            "format",
            [
                ["run", "--offline", "--frozen", "ruff", "format", "src", "tests"],
                ["run", "--offline", "--frozen", "ruff", "check", "--fix", "src", "tests"],
            ],
        ),
        ("lint", [["run", "--offline", "--frozen", "ruff", "check", "src", "tests"]]),
        ("typecheck", [["run", "--offline", "--frozen", "mypy"]]),
        ("test", [["run", "--offline", "--frozen", "pytest"]]),
    ],
)
def test_quality_targets_run_offline_and_frozen(
    tmp_path: Path, target: str, expected: list[list[str]]
) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, target)
    assert result.returncode == 0, result.stderr
    assert _uv_invocations(tmp_path / "uv-args.log") == expected


def test_setup_is_locked(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "setup")
    assert result.returncode == 0, result.stderr
    assert _uv_invocations(tmp_path / "uv-args.log") == [["sync", "--locked"]]
