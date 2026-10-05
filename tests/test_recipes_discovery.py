"""Discovery policy, manual-only generation and state-free CLI behavior (FR-81–85)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grossy.cli import app
from grossy.recipes import DiscoveryError, run_recipes_discover

RUNNER = CliRunner()


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for variable, directory in (
        ("HOME", tmp_path),
        ("XDG_CONFIG_HOME", tmp_path / "config"),
        ("XDG_DATA_HOME", tmp_path / "data"),
        ("XDG_STATE_HOME", tmp_path / "state"),
    ):
        monkeypatch.setenv(variable, str(directory))
    return tmp_path


def test_discover_policy_contract(isolated_home: Path) -> None:
    """Check the public policy once, rather than duplicating individual constant tests."""
    payload = run_recipes_discover("all").to_dict()
    assert payload["status"] == "policy"
    assert payload["target_classes"] == ["meat", "fish", "poultry", "vegetarian"]
    policy = payload["source_policy"]
    assert [(p["tier"], p["scope"]) for p in policy] == [
        ("akis_petretzikis", "greek, european"),
        ("cuisine_native", "cuisine-specific"),
        ("approved_established", "general"),
        ("user", "user-authored"),
        ("ai_generated", "last-resort"),
    ]
    assert set(payload["preferred_cuisines"]) == {
        "greek",
        "indonesian",
        "thai",
        "eastern_european",
        "german",
        "italian",
        "japanese",
        "mexican",
    }
    assert "soft weighting" in payload["cuisine_note"]
    assert "not an eligibility restriction" in payload["cuisine_note"]
    assert "appliance-native" in payload["within_tier_preferences"]
    assert any("appliance-native" in step for step in payload["next_steps"])

    constraints = payload["safety_constraints"]
    assert any("must not be bypassed" in c for c in constraints)
    assert any("No silent substitution" in c for c in constraints)
    assert any("another approved source" in c or "user-authored" in c for c in constraints)
    # Manual last-resort generation is allowed; automatic generation is not.
    assert "explicit manual discovery when no sourced option works" in policy[-1]["description"]
    assert "requires explicit user approval" in policy[-1]["constraint"]
    assert "never automatic" in policy[-1]["constraint"]
    assert any(
        "AI generation requires explicit user approval" in step and "never automatic" in step
        for step in payload["next_steps"]
    )
    assert not list(isolated_home.iterdir())


@pytest.mark.parametrize(
    "classes,expected",
    [("meat", ["meat"]), ("meat,fish", ["meat", "fish"])],
    ids=["single-class", "multiple-classes"],
)
def test_discover_requested_classes(classes: str, expected: list[str]) -> None:
    assert run_recipes_discover(classes).to_dict()["target_classes"] == expected


@pytest.mark.parametrize(
    "classes,error",
    [("invalid", "unknown protein class"), ("", "must not be empty")],
    ids=["unknown-class", "empty-input"],
)
def test_discover_rejects_invalid_classes(classes: str, error: str, isolated_home: Path) -> None:
    with pytest.raises(DiscoveryError, match=error):
        run_recipes_discover(classes)
    assert not list(isolated_home.iterdir())


def test_discover_output_is_deterministic() -> None:
    assert run_recipes_discover("meat").to_dict() == run_recipes_discover("meat").to_dict()


@pytest.mark.parametrize(
    "classes,expected,exit_code",
    [
        ("all", ["meat", "fish", "poultry", "vegetarian"], 0),
        ("vegetarian", ["vegetarian"], 0),
        ("invalid", None, 1),
    ],
    ids=["all-classes", "single-class", "unknown-class"],
)
def test_cli_discover_is_state_free(
    classes: str,
    expected: list[str] | None,
    exit_code: int,
    isolated_home: Path,
) -> None:
    result = RUNNER.invoke(app, ["recipes", "discover", "--from", classes])
    assert result.exit_code == exit_code
    if expected is not None:
        data = json.loads(result.stdout)
        assert data["status"] == "policy"
        assert data["target_classes"] == expected
    assert not list(isolated_home.iterdir())
