"""Tests for the Pi skill contract file (PRD section 9).

Verifies that .pi/skills/grossy/SKILL.md exists, contains all public
commands from PRD 9.2, documents safety boundaries from PRD 9.3, and
includes the observation protocol. No network, browser, or live access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SKILL_PATH = REPO_ROOT / ".pi" / "skills" / "grossy" / "SKILL.md"

# All public skill commands from PRD 9.2
PUBLIC_COMMANDS = [
    "history sync",
    "weekly plan",
    "weekly reroll",
    "weekly add",
    "weekly remove",
    "weekly resume",
    "recipes add",
    "recipes discover",
    "recipes refresh",
]

# Safety boundary requirements from PRD 9.3
SAFETY_REQUIREMENTS = [
    "headed Brave profile",
    "manual authentication",
    "agent-browser",
    "untrusted data",
    "schema-authorized",
    "versioned proposal",
    "deterministic CLI validation",
    "deterministic preflight",
    "mutation manifests",
    "observed state",
    "ambiguity",
    "SQL",
    "credentials",
    "checkout",
    "fulfilment",
    "pay",
]

# Observation protocol requirements
OBSERVATION_REQUIREMENTS = [
    "grossy.cart.observation.v1",
    "schema_name",
    "schema_version",
    "generated_at",
    "product_id",
    "canonical_url",
    "quantity",
    "displayed_price_eur",
]

# Doctor requirements
DOCTOR_REQUIREMENTS = [
    "Configuration",
    "Database",
    "Migrations",
    "Foreign keys",
    "WAL mode",
    "Permissions",
    "Process lock",
    "Brave",
    "agent-browser",
    "Browser profile",
    "Authentication",
    "History controls",
    "Cart read",
]


@pytest.fixture(scope="module")
def skill_content() -> str:
    assert SKILL_PATH.exists(), f"SKILL.md not found at {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


def _command_section(content: str, heading: str) -> str:
    """Extract one '### `...' command section from SKILL.md."""
    marker = f"### `{heading}"
    start = content.index(marker)
    rest = content[start:]
    nxt = rest.find("\n### ", 1)
    return rest[:nxt] if nxt != -1 else rest


def _top_level_section(content: str, heading: str) -> str:
    """Extract one '## ...' top-level section from SKILL.md."""
    marker = f"## {heading}"
    start = content.index(marker)
    rest = content[start:]
    nxt = rest.find("\n## ", 1)
    return rest[:nxt] if nxt != -1 else rest


class TestSkillFileExists:
    def test_file_exists(self) -> None:
        assert SKILL_PATH.exists()

    def test_file_is_non_empty(self, skill_content: str) -> None:
        assert len(skill_content) > 500


class TestPublicCommands:
    """Every supported skill command must be documented in SKILL.md."""

    @pytest.mark.parametrize("command", PUBLIC_COMMANDS)
    def test_command_documented(self, skill_content: str, command: str) -> None:
        assert command in skill_content, f"command {command!r} not found in SKILL.md"


class TestSafetyBoundaries:
    """Safety requirements from PRD 9.3 must be documented."""

    @pytest.mark.parametrize("requirement", SAFETY_REQUIREMENTS)
    def test_safety_requirement_documented(self, skill_content: str, requirement: str) -> None:
        # Case-insensitive check since SKILL.md uses mixed case
        assert requirement.lower() in skill_content.lower(), (
            f"safety requirement {requirement!r} not found in SKILL.md"
        )


class TestObservationProtocol:
    """The observation protocol must document the envelope schema."""

    @pytest.mark.parametrize("field", OBSERVATION_REQUIREMENTS)
    def test_observation_field_documented(self, skill_content: str, field: str) -> None:
        assert field in skill_content, f"observation field {field!r} not in SKILL.md"


class TestDoctorDocumentation:
    """All doctor checks must be documented in SKILL.md."""

    @pytest.mark.parametrize("check", DOCTOR_REQUIREMENTS)
    def test_doctor_check_documented(self, skill_content: str, check: str) -> None:
        assert check.lower() in skill_content.lower(), (
            f"doctor check {check!r} not documented in SKILL.md"
        )


class TestStopConditions:
    """Mandatory stop conditions must be documented."""

    def test_unknown_input_fails_before_browser(self, skill_content: str) -> None:
        content_lower = skill_content.lower()
        assert "unknown" in content_lower or "ambiguous" in content_lower
        assert "before" in content_lower or "fail" in content_lower

    def test_never_enter_credentials(self, skill_content: str) -> None:
        content_lower = skill_content.lower()
        assert "credential" in content_lower
        assert "never" in content_lower

    def test_never_checkout(self, skill_content: str) -> None:
        content_lower = skill_content.lower()
        assert "checkout" in content_lower
        assert "never" in content_lower

    def test_manual_login_instruction(self, skill_content: str) -> None:
        content_lower = skill_content.lower()
        assert "manual" in content_lower or "log in" in content_lower or "login" in content_lower


class TestCommandMapping:
    """Each skill command must map to a deterministic CLI phase."""

    def test_history_sync_maps_to_cli(self, skill_content: str) -> None:
        assert "grossy history discover-prepare" in skill_content
        assert "grossy history sync-dry-run" in skill_content
        assert "grossy history scan-validate" in skill_content
        assert "grossy history sync-apply" in skill_content

    def test_weekly_plan_maps_to_cli(self, skill_content: str) -> None:
        assert "grossy weekly plan-prepare" in skill_content
        assert "grossy weekly baseline-apply" in skill_content

    def test_weekly_add_maps_to_cli(self, skill_content: str) -> None:
        assert "grossy weekly selection-apply" in skill_content
        assert "grossy weekly recipe-preflight" in skill_content

    def test_recipes_add_maps_to_cli(self, skill_content: str) -> None:
        assert "grossy recipes import-validate" in skill_content

    def test_recipes_refresh_maps_to_cli(self, skill_content: str) -> None:
        assert "grossy recipes refresh-diff" in skill_content
        assert "grossy recipes refresh-apply" in skill_content


class TestResumeConfirmationBoundary:
    """weekly resume must present the exact resumed deltas and require explicit
    approval before any resumed cart action (PRD 9.1, 9.3; FR-184)."""

    def test_resume_presents_exact_deltas_for_confirmation(self, skill_content: str) -> None:
        section = _command_section(skill_content, "weekly resume <run-id>").lower()
        assert "resume-prepare" in section
        assert "deltas" in section
        assert "manifest" in section
        assert "confirmation" in section

    def test_resume_aborts_on_decline(self, skill_content: str) -> None:
        section = _command_section(skill_content, "weekly resume <run-id>").lower()
        assert "abort" in section
        assert "no browser mutation" in section

    def test_resume_stops_on_failed_prepare(self, skill_content: str) -> None:
        section = _command_section(skill_content, "weekly resume <run-id>").lower()
        assert "non-zero" in section
        assert "before any browser action" in section


class TestPhaseFailureFailClosed:
    """A failed or incomplete deterministic CLI phase must stop the workflow
    before any browser action (PRD 9.3, 10.3.1)."""

    def test_rule_section_documented(self, skill_content: str) -> None:
        section = _top_level_section(skill_content, "Phase failure rule").lower()
        assert "fail-closed" in section

    def test_nonzero_phase_exit_stops_workflow(self, skill_content: str) -> None:
        section = _top_level_section(skill_content, "Phase failure rule").lower()
        assert "non-zero" in section
        assert "stop" in section
        assert "browser" in section

    def test_missing_expected_output_stops_before_browser(self, skill_content: str) -> None:
        section = _top_level_section(skill_content, "Phase failure rule").lower()
        assert "expected" in section
        assert "manifest" in section
        assert "before any" in section

    def test_safety_boundaries_include_phase_failure(self, skill_content: str) -> None:
        normalized = " ".join(skill_content.lower().split())
        assert (
            "stop the workflow before any browser action whenever a deterministic cli "
            "phase exits non-zero" in normalized
        )
