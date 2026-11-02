"""Portable skill layout and runner-path regressions; no Pi/browser/state activity."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/grossy/SKILL.md"
RUNNER = SKILL.parent / "scripts/live.sh"


def test_pi_settings_discovers_single_canonical_skill_directory() -> None:
    settings_file = ROOT / ".pi/settings.json"
    settings = json.loads(settings_file.read_text())
    # Pi project resource paths are relative to .pi/, not the repository root.
    locations = [(settings_file.parent / path).resolve() for path in settings["skills"]]
    assert locations == [ROOT / "skills"]
    assert list(locations[0].glob("*/SKILL.md")) == [SKILL]
    assert not list((ROOT / ".pi/skills").rglob("SKILL.md"))
    frontmatter = SKILL.read_text().split("---", 2)[1]
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == SKILL.parent.name == "grossy"
    assert metadata["description"]


def test_skill_relative_documentation_links_resolve() -> None:
    for document in (SKILL, SKILL.parent / "scripts/README.md"):
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            path = (document.parent / target.split("#", 1)[0]).resolve()
            assert path.is_relative_to(ROOT), f"link escapes repository: {document}: {target}"
            assert path.exists(), f"broken skill link: {document}: {target}"


@pytest.mark.parametrize(
    "step,arguments,command",
    [
        ("grossy", ["--help"], ["grossy", "--help"]),
        ("start", ["--history-only"], ["python3", "live_start.py", "--history-only"]),
        ("status", [], ["python3", "status.py"]),
    ],
)
def test_runner_resolves_repository_and_scripts_from_unrelated_directory(
    tmp_path: Path, step: str, arguments: list[str], command: list[str]
) -> None:
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    fake_nix = fake_bin / "nix"
    # Record arguments only. Never invoke Nix, Python, Grossy or a live script.
    fake_nix.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake_nix.chmod(0o700)
    expected = list(command)
    if expected[0] == "python3":
        expected[1] = str(RUNNER.parent / expected[1])
    result = subprocess.run(
        [str(RUNNER), step, *arguments],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "develop",
        "--offline",
        "--no-write-lock-file",
        str(ROOT),
        "-c",
        *expected,
    ]
    assert result.stderr == ""
