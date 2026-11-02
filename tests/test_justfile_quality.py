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
    project: Path,
    tmp_path: Path,
    *args: str,
    fail_tool: str | None = None,
    workers: str | None = None,
) -> subprocess.CompletedProcess[str]:
    log = _install_fake_tools(tmp_path)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("GROSSY_TEST_WORKERS", None)
    if workers is not None:
        env["GROSSY_TEST_WORKERS"] = workers
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
    assert _tool_invocations(tmp_path / "tool-args.log") == [
        ("ruff", ["format", "--check", "src", "tests"]),
        ("ruff", ["check", "src", "tests"]),
        ("mypy", []),
        ("python3", ["-m", "pytest", "-n", "auto"]),
    ]


@pytest.mark.parametrize(
    "fail_tool,invocations_before_stop",
    [("ruff", 1), ("mypy", 3)],
    ids=["format-failure", "mypy-failure"],
)
def test_check_short_circuits_on_first_failure(
    tmp_path: Path, fail_tool: str, invocations_before_stop: int
) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "check", fail_tool=fail_tool)
    assert result.returncode != 0
    invocations = _tool_invocations(tmp_path / "tool-args.log")
    assert len(invocations) == invocations_before_stop
    assert invocations[-1][0] == fail_tool


@pytest.mark.parametrize(
    "target,expected",
    [
        (
            "format",
            [("ruff", ["format", "src", "tests"]), ("ruff", ["check", "--fix", "src", "tests"])],
        ),
        ("lint", [("ruff", ["check", "src", "tests"])]),
        ("typecheck", [("mypy", [])]),
        ("test", [("python3", ["-m", "pytest", "-n", "auto"])]),
        ("test-serial", [("python3", ["-m", "pytest", "-n", "0"])]),
    ],
)
def test_single_target_invokes_exactly_its_tools(
    tmp_path: Path, target: str, expected: list[tuple[str, list[str]]]
) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, target)
    assert result.returncode == 0, result.stderr
    assert _tool_invocations(tmp_path / "tool-args.log") == expected


@pytest.mark.parametrize("target", ["test", "check"])
@pytest.mark.parametrize("workers", ["0", "2", "auto", "2; touch SHOULD_NOT_EXIST"])
def test_worker_override_is_passed_as_one_literal_argument(
    tmp_path: Path, target: str, workers: str
) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, target, workers=workers)
    assert result.returncode == 0, result.stderr
    assert _tool_invocations(tmp_path / "tool-args.log")[-1] == (
        "python3",
        ["-m", "pytest", "-n", workers],
    )
    assert not (project / "SHOULD_NOT_EXIST").exists()


def test_cli_worker_override_takes_precedence_over_environment(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "test_workers=2", "test", workers="4")
    assert result.returncode == 0, result.stderr
    assert _tool_invocations(tmp_path / "tool-args.log") == [
        ("python3", ["-m", "pytest", "-n", "2"])
    ]


def test_serial_target_ignores_parallel_environment(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = _run_just(project, tmp_path, "test-serial", workers="4")
    assert result.returncode == 0, result.stderr
    assert _tool_invocations(tmp_path / "tool-args.log") == [
        ("python3", ["-m", "pytest", "-n", "0"])
    ]


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


@pytest.mark.parametrize(
    "recipe,args",
    [
        ("setup", []),
        ("check", []),
        ("test", []),
        ("test-serial", []),
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
