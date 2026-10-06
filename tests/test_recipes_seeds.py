"""Tests for seed recipe import and controlled missing-seed behavior (task 08).

Covers PRD FR-86..FR-88: four approved seed definitions, seed import from
sanitized fixtures, controlled missing-seed reporting (no silent
substitution or AI generation), and idempotent re-import.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.db.migrations import run_migrations
from grossy.init import do_init
from grossy.paths import GrossyPaths
from grossy.recipes import (
    BUNDLED_SEED_FIXTURE_DIR,
    SEED_SPECS,
    SeedImportError,
    run_seed_import,
)
from grossy.recipes.proposal import proposal_checksum
from grossy.recipes.seeds import seed_fixture_path

RUNNER = CliRunner()


# --- seed spec definitions (FR-86) --------------------------------------------------


def test_seed_specs_match_prd_table() -> None:
    """FR-86: exactly four complete seed specs, one per class, with the PRD's URLs."""
    required = {
        "recipe_id",
        "title",
        "reference_url",
        "source_name",
        "source_tier",
        "interpretation",
    }
    assert all(required <= set(spec) for spec in SEED_SPECS)
    assert [(s["protein_class"], s["reference_url"]) for s in SEED_SPECS] == [
        (
            "meat",
            "https://akispetretzikis.com/en/recipe/8684/snitsel-choirino-me-patates-sto-air-fryer",
        ),
        (
            "fish",
            "https://www.foodnetwork.com/recipes/food-network-kitchen/"
            "ginger-and-soy-salmon-fillets-with-broccoli-5277868",
        ),
        ("poultry", "https://www.bbcgoodfood.com/recipes/chicken-alfredo"),
        (
            "vegetarian",
            "https://www.bbcgoodfood.com/recipes/mexican-bean-burgers-lime-yogurt-salsa/",
        ),
    ]


# --- seed fixture helpers --------------------------------------------------------------


def _bean_burgers_fixture() -> dict[str, Any]:
    """The bundled approved sanitized fixture for the spicy bean burgers seed (FR-86).

    Based on the approved source: BBC Good Food Mexican Bean Burgers.
    The recipe makes 6; stored at source scale for linear scaling to 3.
    """
    fixture = seed_fixture_path(BUNDLED_SEED_FIXTURE_DIR, "spicy-bean-burgers")
    return json.loads(fixture.read_text(encoding="utf-8"))


def _import_proposal(recipe: dict[str, Any]) -> dict[str, Any]:
    """Wrap a recipe dict in a checksum-bound import proposal."""
    payload: dict[str, Any] = {
        "schema_name": "grossy.recipe.import.v1",
        "schema_version": 1,
        "generated_at": "2026-09-26T12:00:00+00:00",
        "recipe": recipe,
    }
    checksum_source = {k: v for k, v in payload.items() if k != "checksum"}
    return {**payload, "checksum": proposal_checksum(checksum_source)}


# --- seed import behavior ---------------------------------------------------------------


def _paths(tmp_path: Path) -> GrossyPaths:
    return GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )


def _seed_rows(paths: GrossyPaths) -> dict[str, list[tuple[Any, ...]]]:
    """All persisted seed-domain rows, including mutable fields and revision children."""
    with closing(open_connection(paths.database_file)) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in (
                "recipes",
                "recipe_revisions",
                "recipe_ingredients",
                "recipe_appliance_requirements",
                "recipe_variants",
            )
        }


def _assert_empty_library(paths: GrossyPaths) -> None:
    for table, rows in _seed_rows(paths).items():
        assert rows == [], table


def _assert_bean_record(paths: GrossyPaths) -> None:
    with closing(open_connection(paths.database_file)) as conn:
        assert conn.execute(
            "SELECT recipe_id, protein_class, enabled, is_regular, base_weight FROM recipes"
        ).fetchall() == [("spicy-bean-burgers", "vegetarian", 1, 1, 1.0)]
        assert conn.execute("SELECT provenance FROM recipe_revisions").fetchall() == [
            ("seed-import",)
        ]


@pytest.fixture()
def cli_env(bare_env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch):
    tmp_path, paths = bare_env
    _isolate_home(tmp_path, monkeypatch)
    return tmp_path, paths


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable, root in (
        ("HOME", tmp_path),
        ("XDG_CONFIG_HOME", tmp_path / "config"),
        ("XDG_DATA_HOME", tmp_path / "data"),
        ("XDG_STATE_HOME", tmp_path / "state"),
    ):
        monkeypatch.setenv(variable, str(root))


@pytest.fixture()
def bare_env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    """A migrated database without the init seed bootstrap.

    Used for seed-import behavior tests that must start from an empty
    recipe library (no bundled fixtures imported).
    """
    paths = _paths(tmp_path)
    for directory in (paths.config_dir, paths.data_dir, paths.backups_dir, paths.state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    run_migrations(paths.database_file, paths.backups_dir)
    return tmp_path, paths


def test_seed_import_all_missing_no_silent_generation(
    tmp_path: Path, bare_env: tuple[Path, GrossyPaths]
) -> None:
    """FR-88: With no fixtures, all four seeds report controlled missing state
    and nothing is silently generated or substituted in the database.

    Combines the report assertions and no-generation DB assertion while
    preserving both contracts.
    """
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()

    result = run_seed_import(paths, fixture_dir)
    assert result.imported_count == 0
    assert result.existing_count == 0
    assert result.missing_count == 4
    # All seeds are reported as missing_fixture
    for seed in result.seeds:
        assert seed.status == "missing_fixture"
    # The output explicitly reports the controlled missing seeds
    output = result.to_dict()
    assert len(output["controlled_missing_seeds"]) == 4
    assert "not silently substituted" in output["note"]

    # No silent generation: nothing was created in the database.
    conn = open_connection(paths.database_file)
    try:
        count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_seed_import_with_one_fixture(tmp_path: Path, bare_env: tuple[Path, GrossyPaths]) -> None:
    """One valid fixture imports; the other three remain controlled missing.

    Also folds in the provenance assertion: the imported revision must carry
    the 'seed-import' provenance.
    """
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"
    _write_seed_fixture(fixture_dir, _bean_burgers_fixture()["recipe"])

    result = run_seed_import(paths, fixture_dir)
    assert result.imported_count == 1
    assert result.existing_count == 0
    assert result.missing_count == 3

    # The bean burgers were imported with a revision.
    bean = _seed_status(result, "spicy-bean-burgers")
    assert bean.status == "imported"
    assert bean.revision_id is not None

    # The other three are missing.
    for recipe_id in ("air-fryer-pork-schnitzel", "air-fryer-ginger-soy-salmon", "chicken-alfredo"):
        assert _seed_status(result, recipe_id).status == "missing_fixture"

    # Verify the imported recipe facts and provenance.
    _assert_bean_record(paths)


def test_seed_import_is_idempotent(tmp_path: Path, bare_env: tuple[Path, GrossyPaths]) -> None:
    """Re-import preserves every recipe/revision/child row and user-maintained preferences."""
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"
    _write_seed_fixture(fixture_dir, _bean_burgers_fixture()["recipe"])
    assert run_seed_import(paths, fixture_dir).imported_count == 1
    with closing(open_connection(paths.database_file)) as conn:
        conn.execute(
            "UPDATE recipes SET base_weight = 2.5, is_favorite = 1, is_regular = 0, "
            "enabled = 0 WHERE recipe_id = 'spicy-bean-burgers'"
        )
        conn.commit()
        assert conn.execute(
            "SELECT base_weight, is_favorite, is_regular, enabled FROM recipes"
        ).fetchall() == [(2.5, 1, 0, 0)]
    before = _seed_rows(paths)
    result = run_seed_import(paths, fixture_dir)
    assert result.imported_count == 0
    assert result.existing_count == 1
    assert result.missing_count == 3
    assert _seed_status(result, "spicy-bean-burgers").status == "already_exists"
    assert _seed_rows(paths) == before


def test_seed_import_invalid_fixture(tmp_path: Path, bare_env: tuple[Path, GrossyPaths]) -> None:
    """An invalid fixture file is reported, not silently skipped or substituted."""
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"
    seeds_dir = fixture_dir / "seeds"
    seeds_dir.mkdir(parents=True)

    # Write an invalid fixture (not valid JSON)
    (seeds_dir / "spicy-bean-burgers.json").write_text("not json", encoding="utf-8")

    result = run_seed_import(paths, fixture_dir)
    bean_status = [s for s in result.seeds if s.recipe_id == "spicy-bean-burgers"][0]
    assert bean_status.status == "fixture_invalid"
    assert "validation failed" in bean_status.detail


def test_seed_import_requires_database(tmp_path: Path) -> None:
    """Seed import fails if the database doesn't exist."""
    paths = _paths(tmp_path)
    # No do_init: database doesn't exist
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    with pytest.raises(SeedImportError, match="database not found"):
        run_seed_import(paths, fixture_dir)


def test_seed_fixture_path_is_deterministic(tmp_path: Path) -> None:
    """The fixture path is deterministic: <fixture_dir>/seeds/<recipe_id>.json."""
    expected = seed_fixture_path(tmp_path, "spicy-bean-burgers")
    assert expected == tmp_path / "seeds" / "spicy-bean-burgers.json"


# --- fresh installation seed bootstrap (FR-86, FR-88) ----------------------------------


def test_fresh_init_bootstraps_bundled_seeds(tmp_path: Path) -> None:
    """Fresh init bootstraps the bundled seed and reports the rest (FR-88)."""
    paths = _paths(tmp_path)
    result = do_init(paths)

    seeds = result["recipe_seeds"]
    assert seeds["imported_count"] == 1
    assert seeds["existing_count"] == 0
    assert seeds["missing_count"] == 3
    assert seeds["controlled_missing_seeds"] == [
        "air-fryer-pork-schnitzel",
        "air-fryer-ginger-soy-salmon",
        "chicken-alfredo",
    ]
    statuses = {s["recipe_id"]: s["status"] for s in seeds["seeds"]}
    assert statuses["spicy-bean-burgers"] == "imported"
    for missing_id in seeds["controlled_missing_seeds"]:
        assert statuses[missing_id] == "missing_fixture"
    assert "not silently substituted" in seeds["note"]

    # The bootstrapped seed is a valid revisioned record in the library.
    _assert_bean_record(paths)
    for seed in seeds["seeds"]:
        if seed["status"] == "missing_fixture":
            assert "import-validate" in seed["detail"]


# --- local helpers for seed fixture tests ----------------------------------------


def _write_seed_fixture(
    fixture_dir: Path, recipe: dict[str, Any], name: str = "spicy-bean-burgers.json"
) -> None:
    """Write a single seed proposal to the fixture directory."""
    seeds_dir = fixture_dir / "seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    (seeds_dir / name).write_text(
        json.dumps(_import_proposal(recipe), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _seed_status(result: Any, recipe_id: str) -> Any:
    """Retrieve the seed status by recipe ID from an import result."""
    return [s for s in result.seeds if s.recipe_id == recipe_id][0]


# --- approved-spec mismatch rejection (FR-86) ---------------------------------------------


@pytest.mark.parametrize(
    "field,value,expected_match",
    [
        ("recipe_id", "other-bean-burgers", "recipe_id"),
        ("title", "Completely Different Name", "title"),
        ("reference_url", "https://www.bbcgoodfood.com/recipes/other-bean-recipe", "reference_url"),
        ("base_weight", 2.0, "base weight"),
        ("enabled", False, "must be enabled"),
        ("is_regular", False, "must be a regular recipe"),
    ],
    ids=[
        "recipe_id_mismatch",
        "title_mismatch",
        "reference_url_mismatch",
        "base_weight_mismatch",
        "disabled_fixture_rejected",
        "non_regular_fixture_rejected",
    ],
)
def test_seed_fixture_mismatch_rejected(
    tmp_path: Path,
    bare_env: tuple[Path, GrossyPaths],
    field: str,
    value: Any,
    expected_match: str,
) -> None:
    """A fixture with a mismatched approved-spec field is rejected.

    Parameterized over recipe_id, title, reference_url, base_weight,
    enabled, and is_regular (the source has separate guards for the last
    two). Each rejection must leave no persisted recipe row.
    """
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"
    recipe = dict(_bean_burgers_fixture()["recipe"])
    recipe[field] = value
    _write_seed_fixture(fixture_dir, recipe)

    result = run_seed_import(paths, fixture_dir)
    bean = _seed_status(result, "spicy-bean-burgers")
    assert bean.status == "fixture_invalid"
    assert expected_match in bean.detail
    assert result.imported_count == 0

    # Nothing was imported: no recipe/revision/ingredient/appliance/variant rows.
    _assert_empty_library(paths)


def test_seed_fixture_missing_required_interpretation_rejected(
    tmp_path: Path, bare_env: tuple[Path, GrossyPaths]
) -> None:
    """A salmon fixture without the approved teriyaki/rice facts is rejected.

    The rejection must leave no persisted recipe/revision/child state.
    """
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"

    recipe: dict[str, Any] = {
        "recipe_id": "air-fryer-ginger-soy-salmon",
        "title": "Air-fryer ginger-and-soy salmon with broccoli",
        "reference_url": (
            "https://www.foodnetwork.com/recipes/food-network-kitchen/"
            "ginger-and-soy-salmon-fillets-with-broccoli-5277868"
        ),
        "source_name": "Food Network",
        "source_tier": "approved_established",
        "protein_class": "fish",
        "total_time_minutes": 40,
        "servings": 3.0,
        "ingredients": [
            {"name": "salmon fillet", "amount": 450.0, "unit": "g"},
            {"name": "ginger", "amount": 10.0, "unit": "g"},
            # No teriyaki sauce, no rice: the approved revision facts are missing.
        ],
        "steps": ["Air-fry the salmon with broccoli"],
        "appliances": [{"type": "air_fryer", "capability": "air_frying"}],
        "variants": [],
        "base_weight": 1.0,
        "is_favorite": False,
        "is_regular": True,
        "enabled": True,
    }
    _write_seed_fixture(fixture_dir, recipe, "air-fryer-ginger-soy-salmon.json")

    result = run_seed_import(paths, fixture_dir)
    salmon = _seed_status(result, "air-fryer-ginger-soy-salmon")
    assert salmon.status == "fixture_invalid"
    assert "teriyaki" in salmon.detail
    assert "rice" in salmon.detail
    assert result.imported_count == 0

    # No persisted state was created for the rejected recipe.
    _assert_empty_library(paths)


# --- savepoint rollback on storage error (FR-86, FR-88) ---------------------------------


def test_seed_dangling_variant_rolls_back_partial_insert(
    tmp_path: Path, bare_env: tuple[Path, GrossyPaths]
) -> None:
    """A fixture with an unknown variant target must not leave a partial recipe row.

    The savepoint ensures that if insert_recipe_with_revision fails after
    inserting the recipes row (due to a dangling variant reference), the
    partial state is rolled back and no orphan recipe row remains.
    """
    _, paths = bare_env
    fixture_dir = tmp_path / "fixtures"
    recipe = dict(_bean_burgers_fixture()["recipe"])
    recipe["variants"] = [{"linked_recipe_id": "nonexistent-recipe", "variant_type": "air_fryer"}]
    _write_seed_fixture(fixture_dir, recipe)

    result = run_seed_import(paths, fixture_dir)
    bean = _seed_status(result, "spicy-bean-burgers")
    assert bean.status == "fixture_invalid"
    assert "storage error" in bean.detail
    assert result.imported_count == 0

    # The partial state must be fully rolled back: no recipe/revision/
    # ingredient/appliance/variant rows remain.
    _assert_empty_library(paths)

    # Re-import with a valid fixture must succeed (not blocked by orphan state).
    valid_recipe = dict(_bean_burgers_fixture()["recipe"])
    _write_seed_fixture(fixture_dir, valid_recipe)
    result2 = run_seed_import(paths, fixture_dir)
    assert _seed_status(result2, "spicy-bean-burgers").status == "imported"


# --- CLI integration -------------------------------------------------------------------


def test_cli_seed_with_bundled_fixture(cli_env: tuple[Path, GrossyPaths]) -> None:
    """CLI performs a real import into a migrated, unseeded database."""
    _, paths = cli_env
    result = RUNNER.invoke(app, ["recipes", "seed", "--fixture-dir", str(BUNDLED_SEED_FIXTURE_DIR)])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["status"] == "complete"
    assert data["imported_count"] == 1
    assert data["existing_count"] == 0
    assert data["missing_count"] == 3
    _assert_bean_record(paths)


def test_cli_seed_empty_fixture_dir(cli_env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = cli_env
    empty_dir = tmp_path / "empty-fixtures"
    empty_dir.mkdir()
    result = RUNNER.invoke(app, ["recipes", "seed", "--fixture-dir", str(empty_dir)])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["status"] == "complete"
    assert data["imported_count"] == 0
    assert data["missing_count"] == 4
    assert len(data["controlled_missing_seeds"]) == 4
    _assert_empty_library(paths)


def test_cli_seed_missing_database_leaves_no_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI seed with no database fails and leaves DB/lock absent.

    A seed command against a path where no database exists must fail
    gracefully without creating a database file or lock file.
    """
    paths = _paths(tmp_path)
    # Create directories but do NOT run migrations (no database).
    for directory in (paths.config_dir, paths.data_dir, paths.state_dir):
        directory.mkdir(parents=True, exist_ok=True)

    _isolate_home(tmp_path, monkeypatch)

    empty_dir = tmp_path / "empty-fixtures"
    empty_dir.mkdir()
    result = RUNNER.invoke(app, ["recipes", "seed", "--fixture-dir", str(empty_dir)])
    assert result.exit_code == 1

    # The database file must not have been created.
    assert not paths.database_file.exists(), "seed CLI created a database on failure"
    # The lock file must not have been created.
    lock_file = paths.state_dir / "writer.lock"
    assert not lock_file.exists(), "seed CLI created a lock file on failure"
