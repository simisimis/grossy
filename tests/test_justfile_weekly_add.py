"""End-to-end tests of the Justfile ``weekly-add`` target with a mock pi.

Runs the repository Justfile with a mock ``pi`` executable on PATH that
records its argv; the real Pi is never installed or invoked. This proves the
documented invocation ``just weekly-add RUN_ID=run-1 RECIPES=recipe1``
renders the exact skill prompt, and that hostile argument strings are
rejected before ``pi`` ever launches (no shell evaluation, no real Pi, no
network, no user-state mutation).

Skipped when ``just`` or ``uv`` is not on PATH (outside the Nix development
shell).
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
    shutil.which("just") is None or shutil.which("uv") is None,
    reason="just/uv only available inside the Nix development shell",
)


def _install_mock_pi(tmp_path: Path) -> Path:
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    log = tmp_path / "pi-args.log"
    script = mock_dir / "pi"
    script.write_text(
        "#!/bin/sh\n"
        f': > "{log}"\n'
        'for arg in "$@"; do\n'
        f'    printf "%s\\n" "$arg" >> "{log}"\n'
        "done\n"
        "exit 0\n"
    )
    script.chmod(stat.S_IRWXU)
    return log


def _run_just(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    _install_mock_pi(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["just", "weekly-add", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_documented_invocation_renders_exact_prompt(tmp_path: Path) -> None:
    result = _run_just(tmp_path, "RUN_ID=run-1", "RECIPES=recipe1")
    assert result.returncode == 0, result.stderr
    log = tmp_path / "pi-args.log"
    assert log.read_text().splitlines() == [
        "-p",
        "/skill:grossy weekly add run-1 recipe1",
    ]


def test_comma_list_invocation_renders_prompt(tmp_path: Path) -> None:
    result = _run_just(tmp_path, "RUN_ID=run-1", "RECIPES=recipe1,recipe2")
    assert result.returncode == 0, result.stderr
    log = tmp_path / "pi-args.log"
    assert log.read_text().splitlines() == [
        "-p",
        "/skill:grossy weekly add run-1 recipe1,recipe2",
    ]


def test_hostile_argument_never_reaches_pi(tmp_path: Path) -> None:
    result = _run_just(tmp_path, "RUN_ID=$(printf INJECTED)", "RECIPES=all")
    assert result.returncode != 0
    assert not (tmp_path / "pi-args.log").exists()


def test_hostile_recipe_list_never_reaches_pi(tmp_path: Path) -> None:
    result = _run_just(tmp_path, "RUN_ID=run-1", "RECIPES=recipe1;reboot")
    assert result.returncode != 0
    assert not (tmp_path / "pi-args.log").exists()
