"""Regression tests for the Nix-shell Justfile quality recipes (PRD 16.2, 22.14).

Copies the repository Justfile into an isolated temporary project and runs
the real recipes against a controlled set of fake executables (ruff, mypy,
pytest, python3) on PATH that record every invocation. This proves:

- ``check`` runs format-verification, lint, typecheck, and tests in order
  and short-circuits on first failure;
- ``setup`` verifies provenance without creating .venv or downloading;
- no target invokes uv, pip, or any installer.

The real project manifests are only read, never touched; the fake tools never
access the network or live services. Requires the Nix development shell.
Ordering uses a test-only copy seam;
production recipes must reject fake tools.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _require_just():
    assert shutil.which("just"), "enter the Nix development shell"


# Generic fake tool: logs "TOOLNAME\targ1\targ2..." to $FAKE_TOOL_LOG.
# If $FAKE_FAIL_<TOOLNAME_UPPER> is "1", exits 1.
FAKE_TOOL_TEMPLATE = """\
#!/usr/bin/env bash
TOOL=$(basename "$0")
{
    printf '%s' "$TOOL"
    for arg in "$@"; do
        printf '\\t%s' "$arg"
    done
    printf '\\n'
} >> "$FAKE_TOOL_LOG"
FAIL_VAR="FAKE_FAIL_$(echo "$TOOL" | tr 'a-z' 'A-Z')"
if [ "${!FAIL_VAR:-}" = "1" ]; then
    exit 1
fi
exit 0
"""


def _install_fake_tools(tmp_path: Path) -> Path:
    """Install fake ruff, mypy, pytest, and python3 on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "tool-args.log"

    for tool in ["ruff", "mypy", "pytest", "python3"]:
        script = bin_dir / tool
        script.write_text(FAKE_TOOL_TEMPLATE)
        script.chmod(stat.S_IRWXU)

    return log


def _make_project(tmp_path: Path, *, ordering_harness: bool = True) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    justfile = (REPO_ROOT / "Justfile").read_text(encoding="utf-8")
    if ordering_harness:
        # Test-only copy seam: retain every recipe/dependency, replace only setup.
        # The production file has no bypass flag and is tested separately below.
        start = justfile.index("\nsetup:")
        end = justfile.index("\nformat:", start)
        justfile = justfile[:start] + "\nsetup:\n    @true\n" + justfile[end:]
    (project / "Justfile").write_text(justfile)
    # Minimal src/tests directories so tools don't fail on missing paths.
    (project / "src").mkdir()
    (project / "src" / "__init__.py").write_text("")
    (project / "tests").mkdir()
    (project / "tests" / "__init__.py").write_text("")
    return project


def _run_just(
    project: Path, tmp_path: Path, *args: str, fail_tool: str | None = None
) -> subprocess.CompletedProcess[str]:
    log = _install_fake_tools(tmp_path)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_TOOL_LOG"] = str(log)
    if fail_tool:
        env[f"FAKE_FAIL_{fail_tool.upper()}"] = "1"
    return subprocess.run(
        ["just", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _tool_invocations(log: Path) -> list[tuple[str, list[str]]]:
    """Parse the tool log into (tool_name, args) tuples."""
    if not log.exists():
        return []
    invocations = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        parts = line.split("\t")
        tool = parts[0]
        args = parts[1:]
        invocations.append((tool, args))
    return invocations


def test_check_runs_all_quality_steps_in_order(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "check")
    assert result.returncode == 0, result.stderr
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    # ruff format --check
    assert invocations[0] == ("ruff", ["format", "--check", "src", "tests"])
    # ruff check
    assert invocations[1] == ("ruff", ["check", "src", "tests"])
    # mypy
    assert invocations[2] == ("mypy", [])
    # python3 -m pytest
    assert invocations[3] == ("python3", ["-m", "pytest"])
    # No uv, pip, or installer invocations
    for tool, _ in invocations:
        assert tool not in ("uv", "pip"), f"installer {tool} was invoked"


def test_check_stops_on_format_failure(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "check", fail_tool="ruff")
    assert result.returncode != 0
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    # Only the format check ran before failure
    assert len(invocations) == 1
    assert invocations[0][0] == "ruff"


def test_check_stops_on_mypy_failure(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "check", fail_tool="mypy")
    assert result.returncode != 0
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    # ruff format, ruff check ran; mypy failed; pytest did not run
    assert len(invocations) == 3
    assert invocations[2][0] == "mypy"


def test_format_target_runs_ruff(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "format")
    assert result.returncode == 0, result.stderr
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    assert invocations[0] == ("ruff", ["format", "src", "tests"])
    assert invocations[1] == ("ruff", ["check", "--fix", "src", "tests"])


def test_lint_target_runs_ruff_check(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "lint")
    assert result.returncode == 0, result.stderr
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    assert invocations == [("ruff", ["check", "src", "tests"])]


def test_typecheck_target_runs_mypy(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "typecheck")
    assert result.returncode == 0, result.stderr
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    assert invocations == [("mypy", [])]


def test_test_target_runs_pytest(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "test")
    assert result.returncode == 0, result.stderr
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    # python3 -m pytest
    assert any(
        tool == "python3" and "-m" in args and "pytest" in args for tool, args in invocations
    )


def test_setup_fails_with_non_store_tools(tmp_path: Path) -> None:
    """setup must reject fake (non-store) tools via the provenance guard."""
    project = _make_project(tmp_path, ordering_harness=False)
    result = _run_just(project, tmp_path, "setup")
    assert result.returncode != 0, (
        f"setup should fail when tools are not Nix store paths; got: {result.stdout}"
    )
    assert "not a Nix store path" in result.stdout or "not a Nix store path" in result.stderr, (
        f"Expected provenance error, got: {result.stdout} {result.stderr}"
    )


def test_setup_rejects_venv_in_path(tmp_path: Path) -> None:
    """setup must reject PATH entries containing .venv/bin."""
    project = _make_project(tmp_path, ordering_harness=False)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    # Create a fake .venv/bin on PATH before the fake tools
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    env["PATH"] = f"{venv_bin}{os.pathsep}{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_TOOL_LOG"] = str(tmp_path / "tool-args.log")
    result = subprocess.run(
        ["just", "setup"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, f"setup should fail with .venv in PATH: {result.stdout}"
    assert ".venv" in result.stdout or ".venv" in result.stderr


def test_setup_rejects_virtual_env_var(tmp_path: Path) -> None:
    """setup must reject when VIRTUAL_ENV is set."""
    project = _make_project(tmp_path, ordering_harness=False)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_TOOL_LOG"] = str(tmp_path / "tool-args.log")
    env["VIRTUAL_ENV"] = "/fake/venv"
    result = subprocess.run(
        ["just", "setup"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, f"setup should fail with VIRTUAL_ENV set: {result.stdout}"
    assert "VIRTUAL_ENV" in result.stdout or "VIRTUAL_ENV" in result.stderr


@pytest.mark.parametrize(
    "recipe,args",
    [
        ("check", []),
        ("test", []),
        ("format", []),
        ("lint", []),
        ("typecheck", []),
        ("history-sync", []),
        ("weekly-plan", []),
        ("weekly-add", ["run-1", "all"]),
    ],
)
def test_production_entrypoints_reject_fake_tools(tmp_path, recipe, args):
    project = _make_project(tmp_path, ordering_harness=False)
    result = _run_just(project, tmp_path, recipe, *args)
    assert result.returncode != 0
    assert "not declared project shell tool" in result.stdout
    assert _tool_invocations(tmp_path / "tool-args.log") == []


@pytest.mark.parametrize(
    "variable,value",
    [
        ("PYTHONHOME", "/hostile"),
        ("VIRTUAL_ENV", "/hostile"),
        ("PYTHONPATH", "/hostile"),
        ("PYTHONNOUSERSITE", "0"),
        ("GROSSY_SHELL_PROJECT", "other-project"),
        ("GROSSY_SHELL_PYTHON", "/nix/store/wrong-python-env"),
    ],
)
def test_production_guard_rejects_changed_environment(tmp_path, variable, value):
    project = _make_project(tmp_path, ordering_harness=False)
    env = {**os.environ, variable: value}
    # pytest's intentional source path is not the copied project's source path.
    if variable != "PYTHONPATH":
        env.pop("PYTHONPATH", None)
    result = subprocess.run(
        ["just", "setup"], cwd=project, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0
    assert "ERROR:" in result.stdout
