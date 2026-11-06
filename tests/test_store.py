"""Tests for the ``GROSSY_STORE_URL`` setting reader (``grossy.store``)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from grossy.cli import STATUS_SCHEMA_NAME, app
from grossy.store import STORE_URL_ENV, StoreConfigError, store_base_url, store_host

RUNNER = CliRunner()


def test_missing_variable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STORE_URL_ENV)
    with pytest.raises(StoreConfigError, match=f"{STORE_URL_ENV} is not set") as excinfo:
        store_base_url()
    assert "https://HOST" in str(excinfo.value)
    with pytest.raises(StoreConfigError):
        store_host()


def test_bare_https_host_is_accepted() -> None:
    assert store_base_url() == "https://store.example"
    assert store_host() == "store.example"


def test_explicit_port_is_part_of_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STORE_URL_ENV, "https://store.example:8443")
    assert store_base_url() == "https://store.example:8443"
    assert store_host() == "store.example:8443"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("http://store.example", id="http-scheme"),
        pytest.param("https://store.example/", id="trailing-slash"),
        pytest.param("https://store.example/en", id="path"),
        pytest.param("https://store.example?x=1", id="query"),
        pytest.param("https://store.example#top", id="fragment"),
        pytest.param("https://user:pw@store.example", id="user-info"),
        pytest.param("https://store.example:abc", id="non-numeric-port"),
        pytest.param("https://", id="no-host"),
    ],
)
def test_malformed_value_fails_closed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STORE_URL_ENV, value)
    with pytest.raises(StoreConfigError) as excinfo:
        store_base_url()
    message = str(excinfo.value)
    assert STORE_URL_ENV in message and "https://HOST" in message


def test_value_is_read_at_use_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert store_host() == "store.example"
    monkeypatch.setenv(STORE_URL_ENV, "https://other.example")
    assert store_host() == "other.example"
    monkeypatch.setenv(STORE_URL_ENV, "https://store.example")
    assert store_host() == "store.example"


def test_cli_fails_closed_without_store_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STORE_URL_ENV)
    result = RUNNER.invoke(app, ["history", "product-register", "https://store.example/x/p/1"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema_name"] == STATUS_SCHEMA_NAME
    assert payload["status"] == "error"
    assert STORE_URL_ENV in payload["detail"]
    assert STORE_URL_ENV in result.stderr


def test_cli_doctor_still_runs_without_store_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STORE_URL_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent/grossy-test/config")
    monkeypatch.setenv("XDG_DATA_HOME", "/nonexistent/grossy-test/data")
    monkeypatch.setenv("XDG_STATE_HOME", "/nonexistent/grossy-test/state")
    result = RUNNER.invoke(app, ["doctor"])
    assert result.exit_code == 1
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["store_url"]["status"] == "fail"
    assert STORE_URL_ENV in checks["store_url"]["detail"]
