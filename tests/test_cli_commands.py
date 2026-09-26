"""Tests for CLI command discovery and fail-closed placeholder behavior.

Asserts that the complete deterministic command tree from PRD sections
10.1-10.2 is declared, and that placeholder commands emit versioned JSON on
stdout, diagnostics on stderr, and a non-zero exit code (PRD 10.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from grossy.cli import STATUS_SCHEMA_NAME, STATUS_SCHEMA_VERSION, app

RUNNER = CliRunner(mix_stderr=False)

EXPECTED_COMMANDS = frozenset(
    {
        "init",
        "doctor",
        "history set-primary",
        "history set-recurrence",
        "history set-quantity",
        "history disable",
        "history enable",
        "history blacklist product",
        "history blacklist family",
        "history unblacklist product",
        "history unblacklist family",
        "history family link",
        "history family unlink",
        "history family merge",
        "history set-category",
        "history category rename",
        "history discover-prepare",
        "history sync-dry-run",
        "history scan-validate",
        "history sync-apply",
        "recipes add-file",
        "recipes favorite",
        "recipes regular",
        "recipes weight",
        "recipes enable",
        "recipes disable",
        "recipes import-validate",
        "recipes import-apply",
        "recipes refresh-diff",
        "recipes refresh-apply",
        "weekly finish",
        "weekly abandon",
        "weekly plan-prepare",
        "weekly baseline-apply",
        "weekly recurring-preflight",
        "weekly recurring-intent",
        "weekly recurring-observe",
        "weekly recipes-apply",
        "weekly reroll-apply",
        "weekly selection-apply",
        "weekly recipe-preflight",
        "weekly recipe-intent",
        "weekly recipe-observe",
        "weekly removal-preflight",
        "weekly removal-intent",
        "weekly removal-observe",
        "weekly resume-prepare",
        "db backup",
        "db export",
        "db restore",
    }
)


def collect_commands(command: click.Command, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if not isinstance(command, click.Group):
        return paths
    for name, sub in command.commands.items():
        path = f"{prefix} {name}".strip()
        if isinstance(sub, click.Group):
            paths |= collect_commands(sub, path)
        else:
            paths.add(path)
    return paths


def test_full_command_tree_is_declared() -> None:
    root = get_command(app)
    assert collect_commands(root) == EXPECTED_COMMANDS


def test_no_purge_or_reset_anywhere() -> None:
    root = get_command(app)
    collected = collect_commands(root)
    assert not any(part in ("purge", "reset") for path in collected for part in path.split())


def test_help_exits_zero() -> None:
    result = RUNNER.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("history", "recipes", "weekly", "db"):
        assert group in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["doctor"],
        ["history", "set-primary", "fam-1", "1234567"],
        ["history", "set-recurrence", "fam-1", "--weeks", "2"],
        ["history", "set-quantity", "fam-1", "2"],
        ["history", "disable", "fam-1"],
        ["history", "enable", "fam-1"],
        ["history", "blacklist", "product", "1234567"],
        ["history", "blacklist", "family", "fam-1"],
        ["history", "unblacklist", "product", "1234567"],
        ["history", "unblacklist", "family", "fam-1"],
        ["history", "family", "link", "1234567", "fam-1"],
        ["history", "family", "unlink", "1234567"],
        ["history", "family", "merge", "fam-1", "fam-2"],
        ["history", "set-category", "fam-1", "breakfast"],
        ["history", "category", "rename", "old", "new"],
        ["history", "discover-prepare"],
        ["recipes", "favorite", "r-1"],
        ["recipes", "regular", "r-1"],
        ["recipes", "weight", "r-1", "1.5"],
        ["recipes", "enable", "r-1"],
        ["recipes", "disable", "r-1"],
        ["weekly", "finish", "run-1"],
        ["weekly", "finish", "run-1", "--no-recipes"],
        ["weekly", "abandon", "run-1"],
        ["weekly", "plan-prepare"],
        ["weekly", "plan-prepare", "--skip-history-sync"],
        ["weekly", "reroll-apply", "run-1", "recipe1", "--from", "meat"],
        ["weekly", "selection-apply", "run-1", "recipe1,recipe2"],
        ["weekly", "removal-preflight", "run-1", "--product", "1234567"],
    ],
)
def test_placeholder_fails_closed_with_versioned_json(args: list[str]) -> None:
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 1
    stdout: dict[str, Any] = json.loads(result.output)
    assert stdout["schema_name"] == STATUS_SCHEMA_NAME
    assert stdout["schema_version"] == STATUS_SCHEMA_VERSION
    assert stdout["status"] == "not_implemented"
    assert isinstance(stdout["command"], str) and stdout["command"].startswith(args[0])
    # Diagnostics go to stderr, not stdout: stdout is exactly one JSON line.
    assert result.output.count("\n") <= 1


def test_missing_proposal_file_fails(tmp_path: Path) -> None:
    result = RUNNER.invoke(app, ["history", "scan-validate", str(tmp_path / "missing.json")])
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "args",
    [
        ["history", "set-recurrence", "fam-1", "--weeks", "zero"],
        ["history", "set-quantity", "fam-1", "0"],
        ["recipes", "weight", "r-1", "0"],
        ["weekly", "removal-preflight", "run-1", "--product", "1", "--quantity", "-2"],
    ],
)
def test_invalid_arguments_fail_non_zero(args: list[str]) -> None:
    result = RUNNER.invoke(app, args)
    assert result.exit_code != 0
