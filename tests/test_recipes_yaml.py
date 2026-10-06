"""Tests for user-authored YAML recipe import (PRD FR-73, FR-75; task 07).

A ``grossy.recipe.yaml.v1`` file imports a recipe without a public URL:
the same deterministic invariants as the URL-import proposal, with
optional source fields (defaulting to the user tier) and verbatim
multi-line steps (no whitespace rewriting). Omitted servings default to
the household target from the configuration.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths
from grossy.recipes import (
    RecipeYamlError,
    get_revision_snapshot,
    load_recipe_yaml,
    run_add_file,
)

RUNNER = CliRunner()

BASE_YAML = """\
schema_name: grossy.recipe.yaml.v1
schema_version: 1
recipe:
  recipe_id: {recipe_id}
  title: "Mama's Sunday roast"
  protein_class: meat
  servings: 4
  ingredients:
    - name: "beef shoulder"
      amount: 1.2
      unit: kg
    - name: "rosemary"
      amount: 2
      unit: sprigs
  steps:
    - "Salt the beef a day ahead."
    - "Roast at 200 C, then rest."
"""


def _yaml(recipe_id: str = "mama-sunday-roast", **overrides: Any) -> str:
    text = BASE_YAML.format(recipe_id=recipe_id)
    for key, value in overrides.items():
        text += f"# {key}={value}\n"
    return text


def _write(tmp_path: Path, text: str, name: str = "recipe.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    return tmp_path, paths


def _db(paths: GrossyPaths) -> sqlite3.Connection:
    return open_connection(paths.database_file)


def _count_recipe(conn: sqlite3.Connection, recipe_id: str) -> int:
    """Count recipe rows for a specific recipe_id (isolates from seeded rows)."""
    return conn.execute(
        "SELECT COUNT(*) FROM recipes WHERE recipe_id = ?", (recipe_id,)
    ).fetchone()[0]


def _count_revisions(conn: sqlite3.Connection, recipe_id: str) -> int:
    """Count revision rows belonging to a specific recipe_id."""
    return conn.execute(
        "SELECT COUNT(*) FROM recipe_revisions r "
        "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = ?",
        (recipe_id,),
    ).fetchone()[0]


# --- strict YAML validation (pure, no DB) --------------------------------------


def test_valid_yaml_parses() -> None:
    recipe = load_recipe_yaml(BASE_YAML.format(recipe_id="mama-sunday-roast"))
    assert recipe.recipe_id == "mama-sunday-roast"
    assert recipe.protein_class == "meat"
    assert recipe.servings == 4.0
    assert recipe.reference_url is None


VALID_YAML = BASE_YAML.format(recipe_id="mama-sunday-roast")


@pytest.mark.parametrize(
    "text,match",
    [
        pytest.param("junk: true\n" + VALID_YAML, "unknown field", id="unknown-top-level-field"),
        pytest.param(
            VALID_YAML.replace("  protein_class: meat\n", "  protein_class: meat\n  mystery: 1\n"),
            "recipe file validation failed",
            id="unknown-nested-field",
        ),
        pytest.param(
            VALID_YAML.replace("grossy.recipe.yaml.v1", "grossy.recipe.yaml.v2"),
            "schema_name must be",
            id="wrong-schema-name",
        ),
        pytest.param(
            VALID_YAML.replace("schema_version: 1", "schema_version: 2"),
            "schema_version must be 1",
            id="wrong-schema-version",
        ),
        pytest.param(
            VALID_YAML.replace(
                '  title: "Mama\'s Sunday roast"\n',
                '  title: "Mama\'s Sunday roast"\n  title: "Another"\n',
            ),
            "duplicate",
            id="duplicate-key",
        ),
        pytest.param(
            VALID_YAML.replace("protein_class: meat", "protein_class: insect"),
            "protein_class must be one of",
            id="invalid-protein-class",
        ),
        pytest.param(
            VALID_YAML.replace("amount: 1.2", "amount: 0"), "greater than 0", id="zero-amount"
        ),
        pytest.param(
            VALID_YAML + "  is_favorite: true\n  is_regular: true\n",
            "mutually exclusive",
            id="favorite-and-regular",
        ),
        pytest.param("recipe: [unclosed", "invalid YAML", id="invalid-yaml"),
        pytest.param("", "empty", id="empty-file"),
        pytest.param(
            VALID_YAML + "  reference_url: http://family.example.com/sunday-roast\n",
            "must be an https URL",
            id="http-url",
        ),
    ],
)
def test_invalid_yaml_rejected(text: str, match: str) -> None:
    with pytest.raises(RecipeYamlError, match=match):
        load_recipe_yaml(text)


def test_optional_url_validated_when_present() -> None:
    text = VALID_YAML + "  reference_url: https://family.example.com/sunday-roast\n"
    recipe = load_recipe_yaml(text)
    assert recipe.reference_url == "https://family.example.com/sunday-roast"


def test_control_character_rejected() -> None:
    # NUL and other C0 controls are rejected by the YAML reader itself...
    with pytest.raises(RecipeYamlError, match="invalid YAML"):
        load_recipe_yaml(
            BASE_YAML.format(recipe_id="mama-sunday-roast").replace(
                "Salt the beef", "Salt\x00the beef"
            )
        )
    # ...while the NEL line separator is normalized to a plain newline by
    # the YAML reader and never persisted as a control character.
    recipe = load_recipe_yaml(
        BASE_YAML.format(recipe_id="mama-sunday-roast").replace("Salt the beef", "Salt\x85the beef")
    )
    assert "\x85" not in recipe.steps[0]
    assert recipe.steps[0] == "Salt the beef a day ahead."


# --- user-file import into the offline library ----------------------------------


def test_yaml_import_without_url_defaults_to_user_tier(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    path = _write(tmp_path, BASE_YAML.format(recipe_id="mama-sunday-roast"))
    result = run_add_file(paths, path)
    assert result.to_dict()["status"] == "applied"
    assert result.recipe_id == "mama-sunday-roast"

    conn = _db(paths)
    try:
        row = conn.execute(
            "SELECT reference_url, source_name, source_tier FROM recipes "
            "WHERE recipe_id = 'mama-sunday-roast'"
        ).fetchone()
        assert row == (None, "user", "user")
        revision = conn.execute(
            "SELECT servings, provenance FROM recipe_revisions r "
            "JOIN recipes p ON p.id = r.recipe_id "
            "WHERE p.recipe_id = 'mama-sunday-roast'"
        ).fetchone()
        assert revision[1] == "user-file"
        # Explicit servings in the file are used as the source servings.
        assert revision[0] == 4.0
    finally:
        conn.close()


def test_yaml_omitted_servings_default_to_household_target(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    text = BASE_YAML.format(recipe_id="mama-sunday-roast").replace("  servings: 4\n", "")
    path = _write(tmp_path, text)
    run_add_file(paths, path)
    conn = _db(paths)
    try:
        # do_init writes the sample config whose household recipe_servings is 3.
        revision = conn.execute(
            "SELECT servings FROM recipe_revisions r "
            "JOIN recipes p ON p.id = r.recipe_id "
            "WHERE p.recipe_id = 'mama-sunday-roast'"
        ).fetchone()
        assert revision[0] == 3.0
    finally:
        conn.close()


def test_yaml_steps_stored_verbatim(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    # A literal newline in a YAML double-quoted scalar is written as the
    # \n escape; it persists verbatim (FR-73) with no whitespace rewriting.
    text = BASE_YAML.format(recipe_id="mama-sunday-roast").replace(
        '    - "Salt the beef a day ahead."\n',
        '    - "Mix until smooth.\\nRest for ten minutes.  Keep  the pan tilted."\n',
    )
    path = _write(tmp_path, text)
    result = run_add_file(paths, path)

    snapshot = get_revision_snapshot(_db(paths), result.revision_id)
    assert snapshot["steps"][0] == "Mix until smooth.\nRest for ten minutes.  Keep  the pan tilted."
    # FR-73: no whitespace rewriting (the double space survives).


def test_yaml_collision_rejected_state_unchanged(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    first = _write(tmp_path, BASE_YAML.format(recipe_id="mama-sunday-roast"), "a.yaml")
    run_add_file(paths, first)
    second = _write(
        tmp_path,
        BASE_YAML.format(recipe_id="mama-sunday-roast").replace(
            "Mama's Sunday roast", "Different roast"
        ),
        "b.yaml",
    )
    with pytest.raises(RecipeYamlError, match="already exists"):
        run_add_file(paths, second)
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "mama-sunday-roast") == 1
        assert _count_revisions(conn, "mama-sunday-roast") == 1
        title = conn.execute(
            "SELECT title FROM recipes WHERE recipe_id = ?", ("mama-sunday-roast",)
        ).fetchone()[0]
        assert title == "Mama's Sunday roast"
    finally:
        conn.close()


def test_yaml_missing_database_fails_closed(tmp_path: Path) -> None:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    path = _write(tmp_path, BASE_YAML.format(recipe_id="mama-sunday-roast"))
    with pytest.raises(RecipeYamlError, match="database not found"):
        run_add_file(paths, path)
    assert not paths.database_file.exists()
    assert not (paths.state_dir / "writer.lock").exists()


# --- CLI wiring -----------------------------------------------------------------


@pytest.fixture()
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, GrossyPaths]:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    for var, root in (
        ("HOME", tmp_path),
        ("XDG_CONFIG_HOME", tmp_path / "config"),
        ("XDG_DATA_HOME", tmp_path / "data"),
        ("XDG_STATE_HOME", tmp_path / "state"),
    ):
        monkeypatch.setenv(var, str(root))
    return tmp_path, paths


def test_cli_add_file_success(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = cli_home
    path = _write(tmp_path, BASE_YAML.format(recipe_id="mama-sunday-roast"))
    result = RUNNER.invoke(app, ["recipes", "add-file", str(path)])
    assert result.exit_code == 0
    stdout = json.loads(result.stdout)
    assert stdout["schema_name"] == "grossy.recipe.add-file"
    assert stdout["status"] == "applied"
    assert stdout["recipe_id"] == "mama-sunday-roast"
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "mama-sunday-roast") == 1
    finally:
        conn.close()


def test_cli_add_file_invalid_fails_closed(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = cli_home
    path = _write(
        tmp_path,
        BASE_YAML.format(recipe_id="mama-sunday-roast").replace(
            "protein_class: meat", "protein_class: insect"
        ),
    )
    result = RUNNER.invoke(app, ["recipes", "add-file", str(path)])
    assert result.exit_code == 1
    stdout = json.loads(result.stdout)
    assert stdout["status"] == "error"
    assert "protein_class" in stdout["detail"]
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "mama-sunday-roast") == 0
    finally:
        conn.close()
