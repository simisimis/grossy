"""Tests for the weekly-add skill grammar (PRD sections 9.2, 16.2).

The documented interface is ``just weekly-add RUN_ID=<id> RECIPES=<list|all>``;
Just passes both ``NAME=value`` tokens as positional recipe arguments and
``grossy.skill`` must strip the prefixes, validate the grammar, and emit the
exact prompt ``pi`` receives. Hostile argument strings must be rejected before
any process is launched. No real Pi, shell, network, or user state is
involved.
"""

from __future__ import annotations

import pytest

from grossy.skill import (
    SkillArgumentError,
    main,
    parse_recipe_list,
    parse_run_id,
    weekly_add_prompt,
)

DOCUMENTED_PROMPT = "/skill:grossy weekly add run-1 recipe1"


class TestDocumentedInterface:
    def test_named_documented_form(self) -> None:
        # The exact documented invocation: just weekly-add RUN_ID=run-1 RECIPES=recipe1
        assert weekly_add_prompt("RUN_ID=run-1", "RECIPES=recipe1") == DOCUMENTED_PROMPT

    def test_bare_positional_form(self) -> None:
        assert weekly_add_prompt("run-1", "recipe1") == DOCUMENTED_PROMPT

    def test_all(self) -> None:
        assert weekly_add_prompt("RUN_ID=run-1", "RECIPES=all") == (
            "/skill:grossy weekly add run-1 all"
        )

    def test_comma_separated_list(self) -> None:
        assert weekly_add_prompt("RUN_ID=run-1", "RECIPES=recipe1,recipe2") == (
            "/skill:grossy weekly add run-1 recipe1,recipe2"
        )

    def test_full_slot_list(self) -> None:
        assert parse_recipe_list("recipe1,recipe2,recipe3,recipe4") == (
            "recipe1",
            "recipe2",
            "recipe3",
            "recipe4",
        )


class TestRunIdGrammar:
    @pytest.mark.parametrize(
        "raw",
        [
            "run-1",
            "run_2",
            "RUN1",
        ],
    )
    def test_valid_run_ids(self, raw: str) -> None:
        assert parse_run_id(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "$(printf INJECTED)",
            "`id`",
            "; rm -rf /",
            "a b",
            "-x",
            "run/1",
            "run\\1",
            "'quoted'",
            '"quoted"',
            "$RUN_ID",
            "RUN_ID=",
            "",
        ],
    )
    def test_hostile_run_ids_rejected(self, raw: str) -> None:
        with pytest.raises(SkillArgumentError):
            parse_run_id(raw)


class TestRecipeListGrammar:
    @pytest.mark.parametrize(
        "raw",
        [
            "recipe1",
            "recipe2",
            "recipe3",
            "recipe4",
            "all",
            "recipe1,recipe2",
        ],
    )
    def test_valid_lists(self, raw: str) -> None:
        parse_recipe_list(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "recipeX",
            "recipe5",
            "ALL",
            "All",
            "all,recipe1",
            "recipe1,all",
            "recipe1,recipe1",
            "recipe1,,recipe2",
            ",recipe1",
            "recipe1,",
            "$(echo x)",
            "; reboot",
            "recipe1;recipe2",
            "RECIPES=",
            "",
        ],
    )
    def test_invalid_lists_rejected(self, raw: str) -> None:
        with pytest.raises(SkillArgumentError):
            parse_recipe_list(raw)


class TestMainExitCodes:
    def test_valid_invocation_emits_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["RUN_ID=run-1", "RECIPES=recipe1"]) == 0
        captured = capsys.readouterr()
        assert captured.out == DOCUMENTED_PROMPT
        assert captured.err == ""

    def test_hostile_invocation_fails_before_prompt(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["RUN_ID=$(printf INJECTED)", "RECIPES=all"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err

    def test_wrong_arity_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["run-1"]) == 2
        assert main(["run-1", "recipe1", "extra"]) == 2
