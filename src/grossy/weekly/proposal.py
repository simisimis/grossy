"""Closed offline ingredient-observation boundary; CLI owns recipe choices."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from grossy.recipes.proposal import RECIPE_ID_PATTERN, proposal_checksum
from grossy.security import ProhibitedContentError, scan_prohibited_content
from grossy.weekly.plan import WeeklyPlanError


class IngredientMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recipe_id: str = Field(pattern=RECIPE_ID_PATTERN)
    revision_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=18)
    ingredient_name: str = Field(min_length=1, max_length=100)
    product_id: str = Field(pattern=r"^[0-9]+$", max_length=64)
    fallback_product_ids: list[str] = Field(default_factory=list, max_length=20)
    availability: Literal["available", "unavailable", "unknown"]
    price_eur: str | None = Field(max_length=30)
    evidence: Literal["exact_product_identity", "exact_family_identity"]
    rationale: str = Field(min_length=1, max_length=200)

    @field_validator("fallback_product_ids")
    @classmethod
    def fallback_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not re.fullmatch(r"[0-9]{1,64}", v) for v in value):
            raise ValueError("fallback IDs must be unique numeric strings")
        return value

    @field_validator("price_eur")
    @classmethod
    def money(cls, value: str | None) -> str | None:
        if value is not None and (
            not re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", value) or Decimal(value) <= 0
        ):
            raise ValueError("price_eur must be a positive two-place decimal string")
        return value


class RecipeOptionsProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["grossy.weekly.recipe-options.v1"]
    schema_version: StrictInt
    generated_at: str
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=100)
    matches: list[IngredientMatch] = Field(max_length=2500)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version")
    @classmethod
    def version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("generated_at")
    @classmethod
    def instant(cls, value: str) -> str:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or stamp.tzinfo is None:
            raise ValueError("generated_at must be an ISO timestamp with timezone")
        return value


def _reject_private_content(value: Any) -> None:
    """Reject obvious private/executable content, including encoded markup."""
    from html import unescape
    from urllib.parse import unquote

    if isinstance(value, str):
        decoded = value
        for _ in range(3):
            decoded = unescape(unquote(decoded))
        if re.search(
            r"(?i)(<[^>]+>|\b(?:authorization|cookie|headers?|password|token|secret|"
            r"address|account|email|screenshot|raw_dom)\s*[:=]|\bBearer\s+\S+|"
            r"/(?:home|Users)/|[A-Z]:[\\/]+Users[\\/]+|ab-profile|"
            r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+)",
            decoded,
        ):
            raise WeeklyPlanError("prohibited private or executable recipe-options content")
        if any(not c.isprintable() for c in value):
            raise WeeklyPlanError("recipe-options text must be printable single-line content")
    elif isinstance(value, dict):
        for child in value.values():
            _reject_private_content(child)
    elif isinstance(value, list):
        for child in value:
            _reject_private_content(child)


def validate_recipe_options(data: dict[str, Any]) -> RecipeOptionsProposal:
    try:
        scan_prohibited_content(data)
        _reject_private_content(data)
        proposal = RecipeOptionsProposal.model_validate(data)
        if (
            proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
            != proposal.checksum
        ):
            raise WeeklyPlanError("recipe-options checksum mismatch")
        identities = [(m.recipe_id, m.revision_id, m.ingredient_name) for m in proposal.matches]
        if len(set(identities)) != len(identities):
            raise WeeklyPlanError("duplicate recipe ingredient match")
        if any(m.product_id in m.fallback_product_ids for m in proposal.matches):
            raise WeeklyPlanError("fallback must differ from preferred product")
        return proposal
    except ValidationError as exc:
        first = exc.errors(include_input=False)[0]
        location = ".".join(str(v) for v in first["loc"])
        raise WeeklyPlanError(f"recipe-options rejected at {location}: {first['msg']}") from exc
    except (ProhibitedContentError, ValueError) as exc:
        raise WeeklyPlanError("recipe-options rejected: prohibited or invalid content") from exc
