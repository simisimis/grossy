"""Compare exact skill invocations with the public runtime Click command tree.

Parse command-local options and positional arguments without executing callbacks
or converting illustrative placeholders. No hand-maintained CLI inventory.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest
from typer.main import get_command

from grossy.cli import app

SKILL_PATH = Path(__file__).resolve().parents[1] / "skills/grossy/SKILL.md"


def _leaves(command: click.Command, prefix: str = "") -> dict[str, click.Command]:
    if isinstance(command, click.Group):
        return {
            path: leaf
            for name, child in command.commands.items()
            for path, leaf in _leaves(child, f"{prefix} {name}".strip()).items()
        }
    return {prefix: command}


def _validate_invocations(content: str, *, complete: bool = True) -> None:
    root = get_command(app)
    leaves = _leaves(root)
    seen = set()
    # Only explicit executable syntax, never headings/natural-language text or
    # /skill:grossy orchestration. Newline-spanning inline invocations are valid.
    for snippet in re.findall(r"(?<!`)`([^`]+)`(?!`)", content):
        snippet = " ".join(snippet.split())
        if not snippet.startswith("grossy "):
            continue
        tokens = shlex.split(snippet.replace("[", "").replace("]", ""))[1:]
        command = root
        path_parts = []
        while isinstance(command, click.Group):
            assert tokens, f"incomplete command: {snippet}"
            name = tokens.pop(0)
            assert name in command.commands, f"unknown command: {' '.join([*path_parts, name])}"
            path_parts.append(name)
            command = command.commands[name]
        path = " ".join(path_parts)
        with click.Context(command) as ctx:
            # Click's own parser handles options/values, nargs, interspersed
            # arguments and unknown flags. Type conversion would reject sample
            # <placeholders>, so requiredness is checked against parsed values.
            try:
                values, extra, _ = command.make_parser(ctx).parse_args(tokens)
            except click.ClickException as exc:
                raise AssertionError(f"{path}: {exc}") from exc
        assert not extra, f"{path}: unexpected arguments {extra}"
        for parameter in command.params:
            if not parameter.required:
                continue
            if isinstance(parameter, click.Option) and parameter.default not in (
                None,
                click.core.UNSET,
            ):
                continue
            value = values.get(parameter.name)
            label = parameter.opts[0] if isinstance(parameter, click.Option) else parameter.name
            assert value is not None and value is not click.core.UNSET and value != (), (
                f"{path}: missing required {label}"
            )
        seen.add(path)
    if complete:
        assert seen == set(leaves), f"undocumented commands: {sorted(set(leaves) - seen)}"


def test_skill_exact_runtime_invocation_parity() -> None:
    _validate_invocations(SKILL_PATH.read_text())


@pytest.mark.parametrize(
    "original,replacement,reason",
    [
        (
            "grossy recipes import-apply <proposal.json> --checksum <proposal_checksum>",
            "grossy recipes import-apply <proposal.json>",
            "missing required --checksum",
        ),
        (
            "grossy recipes import-apply <proposal.json> --checksum <proposal_checksum>",
            "grossy recipes import-apply --checksum <proposal_checksum>",
            "missing required proposal",
        ),
        ("grossy weekly finish <run-id>", "grossy weekly finish", "missing required run_id"),
        (
            "grossy history enable <family-id>",
            "grossy history invent <family-id>",
            "unknown command: history invent",
        ),
    ],
)
def test_skill_mutations_fail_for_command_local_reason(original, replacement, reason) -> None:
    content = SKILL_PATH.read_text()
    assert original in content
    with pytest.raises(AssertionError, match=re.escape(reason)):
        _validate_invocations(content.replace(original, replacement, 1))


def test_required_flag_cannot_be_borrowed_from_another_invocation() -> None:
    with pytest.raises(AssertionError, match="import-apply: missing required --checksum"):
        _validate_invocations(
            "`grossy recipes import-apply <proposal.json>`\n"
            "`grossy recipes refresh-apply <proposal.json> --checksum <checksum>`",
            complete=False,
        )


def test_no_public_browser_options() -> None:
    for path, command in _leaves(get_command(app)).items():
        for parameter in command.params:
            if isinstance(parameter, click.Option):
                assert not any("browser" in flag for flag in parameter.opts), path


def test_skill_safety_and_recovery_contract() -> None:
    content = SKILL_PATH.read_text()
    for required in (
        "grossy.cart.observation.v1",
        "grossy.cart.mutation-manifest.v1",
        "schema_name",
        "schema_version",
        "awaiting-recipe-selection",
        "awaiting-user-verification",
        "report-regenerate",
        "proposal_checksum",
        "authentication loss",
        "anti-bot",
    ):
        assert required in content
    assert "no cli phase launches a browser" in content.lower()
    assert "not `/skill:grossy` workflows" in content
