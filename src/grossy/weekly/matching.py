"""Explicit coordinator-reviewed semantic mappings; no implicit fuzzy matching."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from grossy.config.models import GrossyConfigModel, product_id_from_url
from grossy.history.policy import product_gate
from grossy.paths import GrossyPaths
from grossy.weekly.plan import WeeklyPlanError, _now_utc
from grossy.weekly.proposal import IngredientMatch, RecipeOptionsProposal, validate_recipe_options


def register_catalogue_products(conn: sqlite3.Connection, proposal: RecipeOptionsProposal) -> None:
    """Accept observed identities atomically with mappings, never invent purchases."""
    for product in proposal.catalogue_products:
        if (
            product_id_from_url(product.canonical_url) != product.product_id
            or "?" in product.canonical_url
            or "#" in product.canonical_url
        ):
            raise WeeklyPlanError("catalogue URL must bind the exact canonical product identity")
        age = (
            datetime.now(UTC) - datetime.fromisoformat(product.observed_at.replace("Z", "+00:00"))
        ).total_seconds()
        if not -30 <= age <= 300:
            raise WeeklyPlanError("catalogue identity observation must be fresh")
        existing = conn.execute(
            "SELECT canonical_url FROM products WHERE retailer='ab' AND retailer_product_id=?",
            (product.product_id,),
        ).fetchone()
        if existing:
            if product_id_from_url(existing[0]) != product.product_id:
                raise WeeklyPlanError("conflicting stored catalogue identity")
            continue  # Preserve purchased metadata, family policy and retirement.
        now = _now_utc()
        conn.execute(
            "INSERT INTO products (retailer_product_id,canonical_url,name,brand,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (product.product_id, product.canonical_url, product.title, product.brand, now, now),
        )


def require_history_preference(conn: sqlite3.Connection, match: IngredientMatch) -> None:
    """Compatibility is an explicit agent decision; rank that declared set locally."""
    candidates: list[tuple[int, str]] = []
    for pid in match.compatible_product_ids:
        row = conn.execute(
            "SELECT canonical_url,observation_state,purchase_order_count FROM products "
            "WHERE retailer='ab' AND retailer_product_id=?",
            (pid,),
        ).fetchone()
        if row is None or product_id_from_url(row[0]) != pid:
            raise WeeklyPlanError("semantic review references an unknown or inconsistent product")
        if row[1] != "retired" and not product_gate(conn, pid).is_blocked:
            candidates.append((int(row[2]), pid))
    if not candidates:
        raise WeeklyPlanError("no policy-allowed semantic candidate")
    best = max(count for count, _ in candidates)
    if match.product_id not in {pid for count, pid in candidates if count == best}:
        raise WeeklyPlanError("choose the most frequently purchased compatible allowed product")


def _plan(
    conn: sqlite3.Connection, run_id: str, config: GrossyConfigModel
) -> tuple[int, list[dict[str, Any]]]:
    from grossy.weekly.mutation import _require_current_family_policy
    from grossy.weekly.recipes import _policy

    row = conn.execute(
        "SELECT p.id,p.lifecycle,r.candidates_json,r.policy_json FROM planner_runs p "
        "JOIN run_recipe_plans r ON r.run_id=p.id WHERE p.run_id=?",
        (run_id,),
    ).fetchone()
    if row is None or row[1] != "awaiting-recipe-selection":
        raise WeeklyPlanError("matching requires a frozen plan before recipe selection")
    rid = int(row[0])
    if (
        conn.execute("SELECT 1 FROM run_recipe_selections WHERE run_id=?", (rid,)).fetchone()
        or conn.execute(
            "SELECT 1 FROM run_mutation_manifests WHERE run_id=? AND stage IN ('recipe','removal')",
            (rid,),
        ).fetchone()
    ):
        raise WeeklyPlanError("matching cannot change selected or attempted recipes")
    if row[3] != _policy(config):
        raise WeeklyPlanError("recipe policy changed; mapping refresh cannot override it")
    _require_current_family_policy(conn, rid)
    return rid, mapped_candidates(conn, rid, json.loads(row[2]))


def run_matching_prepare(paths: GrossyPaths, run_id: str) -> dict[str, Any]:
    from grossy.db.lock import WriterLock
    from grossy.weekly.plan import _check_database_exists, _load_config_strict

    _check_database_exists(paths)
    config = _load_config_strict(paths)
    with (
        WriterLock(paths.state_dir),
        closing(sqlite3.connect(paths.database_file.as_uri() + "?mode=ro", uri=True)) as conn,
    ):
        rid, candidates = _plan(conn, run_id, config)
        shown = {
            r[0]
            for r in conn.execute(
                "SELECT recipe_revision_id FROM run_recipe_slots WHERE run_id=?", (rid,)
            )
        }
        products = [
            dict(
                product_id=r[0],
                canonical_url=r[1],
                title=r[2],
                brand=r[3],
                purchase_order_count=r[4],
                retired=r[5] == "retired",
                policy_blocked=product_gate(conn, r[0]).is_blocked,
            )
            for r in conn.execute(
                "SELECT retailer_product_id,canonical_url,name,brand,purchase_order_count,"
                "observation_state FROM products WHERE retailer='ab' "
                "ORDER BY purchase_order_count DESC,retailer_product_id"
            )
        ]
        return {
            "schema_name": "grossy.weekly.matching-prepare.v1",
            "schema_version": 1,
            "status": "ready",
            "run_id": run_id,
            "recipes": [
                {
                    "recipe_id": c["recipe_id"],
                    "revision_id": str(c["revision_id"]),
                    "ingredients": c["preview"]["ingredients"],
                }
                for c in candidates
                if c["revision_id"] in shown
            ],
            "local_products": products,
            "policy": {
                "qualifiers": "flexible",
                "ranking": "recorded order-presence count",
                "disliked_ingredients": config.disliked_ingredients,
            },
        }


def run_matches_apply(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Refresh ingredient previews only, preserving slots/revisions/consent/cart truth."""
    from grossy.weekly.mutation import _event, _locked
    from grossy.weekly.preview import build_recipe_preview
    from grossy.weekly.recipes import _save_reports

    proposal = validate_recipe_options(data)
    if proposal.run_id != run_id or not proposal.matches:
        raise WeeklyPlanError("mapping refresh requires matching run and explicit mappings")
    with _locked(paths) as (conn, config):
        rid, candidates = _plan(conn, run_id, config)
        shown = {
            r[0]
            for r in conn.execute(
                "SELECT recipe_revision_id FROM run_recipe_slots WHERE run_id=?", (rid,)
            )
        }
        by_recipe = {c["recipe_id"]: c for c in candidates if c["revision_id"] in shown}
        for match in proposal.matches:
            c = by_recipe.get(match.recipe_id)
            if (
                c is None
                or str(c["revision_id"]) != match.revision_id
                or match.ingredient_name not in {i["name"] for i in c["ingredients"]}
            ):
                raise WeeklyPlanError(
                    "mapping must bind a shown frozen recipe revision and ingredient"
                )
            current = conn.execute(
                "SELECT current_revision_id,enabled FROM recipes WHERE recipe_id=?",
                (match.recipe_id,),
            ).fetchone()
            if not current or current[0] != c["revision_id"] or not current[1]:
                raise WeeklyPlanError("recipe revision or eligibility changed")
        register_catalogue_products(conn, proposal)
        for match in proposal.matches:
            if match.evidence == "agent_semantic_review":
                require_history_preference(conn, match)
        matches = {(m.recipe_id, m.ingredient_name): m for m in proposal.matches}
        for recipe_id in {m.recipe_id for m in proposal.matches}:
            c = by_recipe[recipe_id]
            prior = {i["name"] for i in c["preview"]["ingredients"] if i["exact_match"] is not None}
            if not prior.issubset({name for recipe, name in matches if recipe == recipe_id}):
                raise WeeklyPlanError("refresh must retain previously mapped ingredients")
            c["match_observation_generated_at"] = proposal.generated_at
            c["preview"] = build_recipe_preview(conn, c, config, matches)
            c["semantic_mapping_checksum"] = proposal.checksum
            conn.execute(
                "INSERT OR IGNORE INTO run_recipe_mappings "
                "(run_id,recipe_revision_id,proposal_checksum,preview_json,"
                "mapping_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    rid,
                    c["revision_id"],
                    proposal.checksum,
                    json.dumps(c["preview"], sort_keys=True),
                    json.dumps(
                        [m.model_dump() for m in proposal.matches if m.recipe_id == recipe_id],
                        sort_keys=True,
                    ),
                    _now_utc(),
                ),
            )
        _event(
            conn,
            rid,
            "recipe-mappings-accepted",
            {
                "checksum": proposal.checksum,
                "generated_at": proposal.generated_at,
                "matches": [m.model_dump() for m in proposal.matches],
                "catalogue_products": [p.model_dump() for p in proposal.catalogue_products],
            },
        )
        return _save_reports(conn, paths, rid, run_id, _now_utc())


def mapped_candidates(
    conn: sqlite3.Connection, rid: int, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay latest append-only mappings; never modify the original plan."""
    version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
    if version < 11:
        return [dict(c) for c in candidates]
    overrides = {
        int(r[0]): (r[1], r[2])
        for r in conn.execute(
            "SELECT recipe_revision_id,preview_json,proposal_checksum FROM run_recipe_mappings "
            "WHERE id IN (SELECT MAX(id) FROM run_recipe_mappings WHERE run_id=? "
            "GROUP BY recipe_revision_id)",
            (rid,),
        )
    }
    result = []
    for candidate in candidates:
        updated = dict(candidate)
        if candidate["revision_id"] in overrides:
            preview, checksum = overrides[candidate["revision_id"]]
            updated["preview"] = json.loads(preview)
            updated["semantic_mapping_checksum"] = checksum
        result.append(updated)
    return result
