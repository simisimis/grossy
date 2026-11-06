"""Private boundary diagnostics and shared human-report minimization, offline only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grossy.browser.observation import CartObservationError, validate_cart_observation
from grossy.cli import app
from grossy.config import GrossyConfigError, load_config_text
from grossy.doctor import DoctorCheckResult, DoctorResult
from grossy.history.sync import _sanitize_untrusted_text
from grossy.security import ProhibitedContentError, sanitize_human_text, scan_prohibited_content
from grossy.weekly.plan import _sanitize_report_field


@pytest.mark.parametrize(
    "private",
    [
        "Cookie: session=synthetic-secret",
        "Authorization: Basic synthetic-secret",
        "Bearer synthetic-secret",
        "password=synthetic-secret",
        "address: synthetic-secret",
        "account: synthetic-secret",
        "fixture@example.invalid",
        "/tmp/fixture/ab-profile/synthetic-secret",
        "<script>synthetic-secret</script>",
    ],
)
def test_prohibited_strings_are_rejected_without_echo(private: str) -> None:
    with pytest.raises(ProhibitedContentError) as error:
        scan_prohibited_content({"title": private})
    assert private not in str(error.value)
    with pytest.raises(CartObservationError) as cart_error:
        validate_cart_observation({"unexpected": private})
    assert private not in str(cart_error.value)
    for sanitize in (_sanitize_untrusted_text, _sanitize_report_field):
        text = sanitize(private + " 987654321", {"987654321"})
        assert private not in text and "987654321" not in text


def test_cli_rejected_observation_never_prints_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_weekly_mutation import home as fixture_home
    from test_weekly_mutation import prepare

    paths = fixture_home.__wrapped__(tmp_path)
    run_id = prepare(paths)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(paths.config_dir.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(paths.data_dir.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(paths.state_dir.parent))
    proposal = tmp_path / "observation.json"
    proposal.write_text(json.dumps({"unexpected": "<script>synthetic-secret</script>"}))
    result = CliRunner().invoke(app, ["weekly", "baseline-apply", run_id, str(proposal)])
    assert result.exit_code != 0
    assert "synthetic-secret" not in result.output


@pytest.mark.parametrize("fact_field", ["recurring_product_facts", "recipe_product_facts"])
def test_cli_malformed_current_fact_price_is_structured_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fact_field: str
) -> None:
    from test_weekly_mutation import home as fixture_home
    from test_weekly_mutation import observation

    from grossy.db.connection import open_connection
    from grossy.weekly.plan import run_plan_prepare

    paths = fixture_home.__wrapped__(tmp_path)
    run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(paths.config_dir.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(paths.data_dir.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(paths.state_dir.parent))
    data = observation({"111": 1}, facts=False)
    fact = {
        "product_id": "111",
        "canonical_url": "https://store.example/en/eshop/food/p/111",
        "displayed_price_eur": "not-a-price",
        "availability": "available",
    }
    if fact_field == "recipe_product_facts":
        fact.update(package_amount="1", package_unit="piece", package_count=1, sale_kind="packaged")
    data[fact_field] = [fact]
    proposal = tmp_path / "observation.json"
    proposal.write_text(json.dumps(data))
    conn = open_connection(paths.database_file)
    try:
        before = list(conn.iterdump())
        artifacts_before = {
            path.relative_to(paths.state_dir): path.read_bytes()
            for path in paths.state_dir.rglob("*")
            if path.is_file()
        }
        result = CliRunner().invoke(app, ["weekly", "baseline-apply", run_id, str(proposal)])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["schema_name"] == "grossy.cli.status"
        assert payload["schema_version"] == 1
        assert payload["status"] == "error"
        assert "displayed_price_eur" in payload["detail"]
        assert "grossy:" in result.stderr
        assert "not-a-price" not in result.stdout + result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        assert list(conn.iterdump()) == before
        assert {
            path.relative_to(paths.state_dir): path.read_bytes()
            for path in paths.state_dir.rglob("*")
            if path.is_file()
        } == artifacts_before
    finally:
        conn.close()


def test_yaml_syntax_diagnostic_omits_source_snippet() -> None:
    from grossy.recipes.yaml_recipe import RecipeYamlError, load_recipe_yaml

    for loader, error_type in (
        (load_config_text, GrossyConfigError),
        (load_recipe_yaml, RecipeYamlError),
    ):
        with pytest.raises(error_type) as error:
            loader('key: ["synthetic-secret"\n')
        assert "synthetic-secret" not in str(error.value)
        assert "line" in str(error.value)


def test_doctor_serialization_redacts_profile_and_adapter_details() -> None:
    result = DoctorResult(
        "fail",
        (
            DoctorCheckResult(
                "browser_profile",
                "fail",
                "/tmp/fixture/ab-profile",
                "chmod /tmp/fixture/ab-profile",
            ),
            DoctorCheckResult("auth", "fail", "Cookie: synthetic-secret"),
        ),
    )
    output = json.dumps(result.to_dict())
    assert "ab-profile" not in output and "synthetic-secret" not in output
    assert sanitize_human_text("ordinary product facts") == "ordinary product facts"


def test_history_duplicate_source_identity_diagnostic_is_count_only() -> None:
    from test_history_sync import _window_listed

    from grossy.history import HistoryProposalError, validate_listed_orders

    data = _window_listed()
    data["orders"][1] = dict(data["orders"][0])
    source_id = data["orders"][0]["source_order_id"]
    with pytest.raises(HistoryProposalError, match="duplicate source order ID") as error:
        validate_listed_orders(data)
    assert source_id not in str(error.value)
