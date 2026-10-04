"""Read-only deterministic weekly recipe planning and rerolls (Task 10)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from decimal import Decimal
from typing import Any

from grossy.config.models import GrossyConfigModel
from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import PROTEIN_CLASSES
from grossy.recipes.repository import get_revision_snapshot
from grossy.recipes.variants import check_appliance_eligibility, get_appliance_config_from_yaml
from grossy.weekly.plan import (
    WeeklyPlanError,
    _check_database_exists,
    _get_source_order_ids,
    _load_config_strict,
    _now_utc,
    _persist_report_recovery,
    _sanitize_report_field,
)
from grossy.weekly.preview import (
    build_recipe_preview,
    ingredient_family_blacklisted,
    ingredient_identity,
    pantry_covers,
    product_identity_valid,
)
from grossy.weekly.proposal import RecipeOptionsProposal, validate_recipe_options
from grossy.weekly.report import build_json_report, build_markdown_report

SLOTS = dict(zip(("recipe1", "recipe2", "recipe3", "recipe4"), PROTEIN_CLASSES, strict=True))
PREFERRED_CUISINES = frozenset(
    {"greek", "indonesian", "thai", "eastern european", "german", "italian", "japanese", "mexican"}
)


def derive_seed(run_id: str, slot: str | None = None, counter: int | None = None) -> str:
    return hashlib.sha256(
        json.dumps([run_id, slot, counter], separators=(",", ":")).encode()
    ).hexdigest()


def effective_weight(
    base: Decimal, selected: bool, nonselected_runs: int, cuisine: str | None
) -> Decimal:
    history = (
        Decimal("0.25") if selected else (Decimal("0.5") if nonselected_runs >= 2 else Decimal(1))
    )
    return (
        base
        * history
        * (
            Decimal("1.25")
            if ingredient_identity(cuisine or "") in PREFERRED_CUISINES
            else Decimal(1)
        )
    )


def weighted_choice(candidates: list[dict[str, Any]], seed: str) -> dict[str, Any] | None:
    ordered = sorted(candidates, key=lambda c: c["recipe_id"])
    if not ordered:
        return None
    total = sum((Decimal(c["weight"]) for c in ordered), Decimal(0))
    point = Decimal(int(seed, 16)) / Decimal(2**256) * total
    for candidate in ordered:
        point -= Decimal(candidate["weight"])
        if point < 0:
            return candidate
    return ordered[-1]


def _policy(config: GrossyConfigModel) -> str:
    return config.model_dump_json()


def _eligibility(
    conn: sqlite3.Connection, snapshot: dict[str, Any], config: GrossyConfigModel
) -> list[str]:
    reasons = []
    time = snapshot["total_time_minutes"]
    if time is None or time > 60:
        reasons.append("total time unknown or greater than 60 minutes")
    if snapshot["protein_class"] not in PROTEIN_CLASSES:
        reasons.append("unsupported protein class")
    if sum(not pantry_covers(i["name"], config) for i in snapshot["ingredients"]) > 15:
        reasons.append("more than 15 non-pantry ingredients")
    dislikes = {ingredient_identity(v) for v in config.disliked_ingredients}
    for ingredient in snapshot["ingredients"]:
        if ingredient_identity(ingredient["name"]) in dislikes:
            reasons.append(f"disliked ingredient: {ingredient['name']}")
        if ingredient_family_blacklisted(conn, ingredient["name"]):
            reasons.append(f"family-blacklisted required ingredient: {ingredient['name']}")
    configured = get_appliance_config_from_yaml(config.appliances.model_dump())
    reasons.extend(
        v["reason"] for v in check_appliance_eligibility(snapshot["appliances"], configured)
    )
    return reasons


def _candidate_snapshots(
    conn: sqlite3.Connection,
    run_row_id: int,
    config: GrossyConfigModel,
    proposal: RecipeOptionsProposal,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    candidates: dict[str, dict[str, Any]] = {}
    exclusions: dict[str, list[str]] = {}
    prior = [
        r[0]
        for r in conn.execute(
            "SELECT run_id FROM planner_runs WHERE id < ? ORDER BY id DESC LIMIT 4", (run_row_id,)
        )
    ]
    matches = {(m.recipe_id, m.ingredient_name): m for m in proposal.matches}
    for match in proposal.matches:
        revision = conn.execute(
            "SELECT r.recipe_id, v.id FROM recipes r JOIN recipe_revisions v ON v.recipe_id=r.id "
            "WHERE r.recipe_id=? AND r.current_revision_id=v.id AND v.id=? AND "
            "v.accepted_at IS NOT NULL",
            (match.recipe_id, int(match.revision_id)),
        ).fetchone()
        if revision is None:
            raise WeeklyPlanError("match references unknown, unaccepted or stale recipe revision")
        names = [
            r[0]
            for r in conn.execute(
                "SELECT ingredient_name FROM recipe_ingredients WHERE revision_id=?",
                (int(match.revision_id),),
            )
        ]
        if match.ingredient_name not in names:
            raise WeeklyPlanError("match ingredient identity does not belong to revision")
        for pid in [match.product_id, *match.fallback_product_ids]:
            row = conn.execute(
                "SELECT canonical_url FROM products WHERE retailer='ab' AND retailer_product_id=?",
                (pid,),
            ).fetchone()
            if row is None or not product_identity_valid(
                {
                    "canonical_url": row[0],
                    "product_id": pid,
                }
            ):
                raise WeeklyPlanError("match product identity is unknown or inconsistent")
    for row in conn.execute(
        "SELECT r.id, r.recipe_id, r.enabled, r.current_revision_id, r.base_weight, "
        "v.recipe_id, v.accepted_at, v.source_name, v.reference_url, v.cuisine "
        "FROM recipes r LEFT JOIN recipe_revisions v ON "
        "v.id=r.current_revision_id ORDER BY r.recipe_id"
    ).fetchall():
        row_id, rid, enabled, revision_id, base, owner, accepted, source, url, cuisine = row
        reasons = []
        if not enabled:
            reasons.append("recipe disabled")
        if revision_id is None or owner != row_id or not accepted:
            reasons.append("no accepted owned local revision; explicit discovery/import required")
        if not math.isfinite(base) or base <= 0:
            reasons.append("invalid base weight")
        if reasons:
            exclusions[rid] = reasons
            continue
        snapshot = get_revision_snapshot(conn, revision_id)
        reasons = _eligibility(conn, snapshot, config)
        if reasons:
            exclusions[rid] = reasons
            continue
        events = conn.execute(
            "SELECT event_type, run_id FROM recipe_events WHERE recipe_id=?", (row_id,)
        ).fetchall()
        selected = {r[1] for r in events if r[0] == "selected" and r[1] in prior}
        suggested = {r[1] for r in events if r[0] == "suggested" and r[1] in prior}
        snapshot.update(
            row_id=row_id,
            run_row_id=run_row_id,
            source={"name": source, "reference_url": url, "tier": snapshot["source_tier"]},
            base_weight=str(base),
            match_observation_generated_at=proposal.generated_at,
            cuisine=cuisine,
            history={
                "previous_runs": prior,
                "recent_selected": bool(selected),
                "nonselected_runs": len(suggested - selected),
            },
        )
        snapshot["preview"] = build_recipe_preview(conn, snapshot, config, matches)
        candidates[rid] = snapshot

    # Linked dishes form undirected components. Resolve preference using FULL
    # eligibility before weighting; one representative prevents duplicate dishes.
    parent = {r[0]: r[0] for r in conn.execute("SELECT recipe_id FROM recipes")}

    def root(rid: str) -> str:
        while parent[rid] != rid:
            rid = parent[rid]
        return rid

    links = conn.execute(
        "SELECT r.recipe_id, l.recipe_id, v.variant_type FROM recipe_variants v "
        "JOIN recipe_revisions rev ON rev.id=v.revision_id JOIN recipes r ON r.id=rev.recipe_id "
        "JOIN recipes l ON l.id=v.linked_recipe_id WHERE r.current_revision_id=rev.id "
        "AND rev.accepted_at != ''"
    ).fetchall()
    ranks = {rid: 2 for rid in candidates}
    for base_id, linked_id, kind in links:
        parent[root(linked_id)] = root(base_id)
        if linked_id in ranks:
            if (
                kind == "air_fryer"
                and config.appliance_preferences.prefer_air_fryer_over_pan_frying
            ):
                ranks[linked_id] = min(ranks[linked_id], 0)
            elif (
                kind == "thermomix_native"
                and config.appliance_preferences.prefer_thermomix_native_over_manual
            ):
                ranks[linked_id] = min(ranks[linked_id], 1)
    groups: dict[str, list[dict[str, Any]]] = {}
    for rid, candidate in candidates.items():
        candidate["dish_id"] = root(rid)
        groups.setdefault(root(rid), []).append(candidate)
    resolved = []
    for group in groups.values():
        best_rank = min(ranks[c["recipe_id"]] for c in group)
        # Equal-rank variants remain weighted candidates; selection uses dish IDs
        # to prevent duplicates across slots/rerolls, not lexical displacement.
        for candidate in group:
            if ranks[candidate["recipe_id"]] == best_rank:
                candidate["weight"] = str(
                    effective_weight(
                        Decimal(candidate["base_weight"]),
                        candidate["history"]["recent_selected"],
                        candidate["history"]["nonselected_runs"],
                        candidate["cuisine"],
                    )
                )
                resolved.append(candidate)
            else:
                exclusions[candidate["recipe_id"]] = [
                    "eligible linked variant preferred before weighting by appliance policy"
                ]
    return sorted(resolved, key=lambda c: c["recipe_id"]), exclusions


def _sanitize_tree(value: Any, order_ids: set[str]) -> Any:
    if isinstance(value, str):
        text = _sanitize_report_field(value, order_ids)
        text = re.sub(r"<[^>]*>", "[redacted-markup]", text)
        text = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[redacted-email]", text)
        text = re.sub(r"\S*ab-profile\S*", "[redacted-path]", text, flags=re.IGNORECASE)
        return re.sub(
            r"(?i)\b(?:address|account|email|headers?|screenshot|raw_dom)\s*[:=].*",
            "[redacted-private-data]",
            text,
        )
    if isinstance(value, list):
        return [_sanitize_tree(v, order_ids) for v in value]
    if isinstance(value, dict):
        return {
            _sanitize_report_field(k, order_ids): _sanitize_tree(v, order_ids)
            for k, v in value.items()
            if k != "steps"
        }
    return value


def _show(
    conn: sqlite3.Connection, row_id: int, run_id: str, candidate: dict[str, Any], now: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO run_recipe_seen VALUES (?, ?, ?)", (row_id, candidate["row_id"], now)
    )
    conn.execute(
        "INSERT INTO recipe_events (recipe_id, event_type, run_id, detail, "
        "created_at) VALUES (?, 'suggested', ?, ?, ?)",
        (candidate["row_id"], run_id, f"revision={candidate['revision_id']}", now),
    )


def _save_reports(
    conn: sqlite3.Connection, paths: GrossyPaths, row_id: int, run_id: str, now: str
) -> dict[str, Any]:
    report_row = conn.execute(
        "SELECT report_json, observation_generated_at FROM weekly_reports WHERE run_id=?", (row_id,)
    ).fetchone()
    data = json.loads(report_row[0])
    plan = conn.execute(
        "SELECT seed, candidates_json, exclusions_json, created_at, proposal_generated_at "
        "FROM run_recipe_plans WHERE run_id=?",
        (row_id,),
    ).fetchone()
    candidates = json.loads(plan[1])
    by_revision = {c["revision_id"]: c for c in candidates}
    slots = []
    for name, protein, revision, reason, seed in conn.execute(
        "SELECT slot_name, protein_class, recipe_revision_id, empty_reason, "
        "seed FROM run_recipe_slots WHERE run_id=? ORDER BY slot_name",
        (row_id,),
    ):
        candidate = by_revision.get(revision)
        slots.append(
            {
                "slot": name,
                "original_class": SLOTS[name],
                "protein_class": protein,
                "seed": seed,
                "empty_reason": reason,
                "preview": candidate["preview"] if candidate else None,
            }
        )
    rerolls = [
        dict(
            zip(
                (
                    "slot",
                    "number",
                    "scope",
                    "previous_recipe_id",
                    "new_recipe_id",
                    "result",
                    "reason",
                ),
                r,
                strict=True,
            )
        )
        for r in conn.execute(
            "SELECT rr.slot_name, rr.reroll_number, rr.candidate_scope, "
            "p.recipe_id, n.recipe_id, rr.result, rr.reason "
            "FROM run_rerolls rr LEFT JOIN recipes p ON "
            "p.id=rr.previous_recipe_id LEFT JOIN recipes n ON "
            "n.id=rr.new_recipe_id WHERE rr.run_id=? ORDER BY rr.id",
            (row_id,),
        )
    ]
    for reroll in rerolls:
        reroll["seed"] = derive_seed(run_id, reroll["slot"], reroll["number"])
    data.update(
        lifecycle="awaiting-recipe-selection",
        seed=plan[0],
        recipe_planned_at=plan[3],
        recipe_observation_generated_at=plan[4],
        recipe_slots=slots,
        reroll_history=rerolls,
        recipe_exclusions=json.loads(plan[2]),
        pantry_assumptions=json.loads(
            conn.execute(
                "SELECT policy_json FROM run_recipe_plans WHERE run_id=?", (row_id,)
            ).fetchone()[0]
        )["pantry_assumptions"],
        recipe_selection_mappings=[],
        mutations=[],
        removals=[],
    )
    known_cost = sum(
        (Decimal(s["preview"]["known_incremental_cost_eur"]) for s in slots if s["preview"]),
        Decimal(0),
    )
    complete = all(s["preview"] is None or s["preview"]["cost_complete"] for s in slots)
    budget = data["budget"]
    total = (
        Decimal(budget.get("resulting_total_eur", budget["baseline_subtotal_eur"]))
        + Decimal(budget["projected_recurring_eur"])
        + known_cost
    )
    budget.update(
        verified_recurring_eur=budget.get("verified_recurring_eur", "0.00"),
        verified_recipe_eur="0.00",
        projected_recipe_eur=f"{known_cost:.2f}" if complete else None,
        known_projected_recipe_eur=f"{known_cost:.2f}",
        recipe_cost_complete=complete,
        projected_total_eur=f"{total:.2f}",
        variance_eur=f"{total - Decimal(budget['weekly_budget_eur']):.2f}",
        assumptions=[
            "sum of independent four-option previews is an upper scenario, not "
            "an authorized selection; shared ingredients not consolidated",
            "unknown costs excluded from known projected total; actual total may be higher",
        ],
    )
    # Rough non-medical coverage: transparent counts of option ingredient identities.
    names = [
        ingredient_identity(i["name"])
        for s in slots
        if s["preview"]
        for i in s["preview"]["ingredients"]
    ]
    groups = {
        "protein": {"chicken", "pork", "beef", "salmon", "fish", "beans", "eggs", "lentils"},
        "carbohydrate": {"rice", "pasta", "potatoes", "bread", "flour"},
        "fibre": {"beans", "lentils", "broccoli", "carrots", "cabbage"},
        "vegetables": {"broccoli", "carrots", "cabbage", "tomatoes", "onion", "spinach"},
    }
    data["rough_coverage"] = {
        key: (
            "low"
            if (count := sum(n in identities for n in names)) < 2
            else "good"
            if count <= 4
            else "excessive"
        )
        for key, identities in groups.items()
    }
    data["coverage_assumptions"] = (
        "rough exact-identity ingredient counts across unselected options: <2 "
        "low, 2–4 good, >4 excessive; incomplete identities may undercount; no "
        "nutrition or medical claims"
    )
    data = _sanitize_tree(data, _get_source_order_ids(conn))
    md, structured = build_markdown_report(data), build_json_report(data)
    md_path, json_path = paths.run_dir(run_id) / "report.md", paths.run_dir(run_id) / "report.json"
    _persist_report_recovery(
        conn, row_id, md, structured, str(md_path), str(json_path), report_row[1], now
    )
    conn.execute(
        "UPDATE planner_runs SET lifecycle='awaiting-recipe-selection', "
        "seed=?, report_md_path=?, report_json_path=?, updated_at=? WHERE id=?",
        (plan[0], str(md_path), str(json_path), now, row_id),
    )
    conn.commit()
    return _write_reports(conn, paths, row_id, run_id, data)


def _write_reports(
    conn: sqlite3.Connection, paths: GrossyPaths, row_id: int, run_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT report_md, report_json FROM weekly_reports WHERE run_id=?", (row_id,)
    ).fetchone()
    try:
        ensure_private_dir(paths.run_dir(run_id))
        atomic_write_text(paths.run_dir(run_id) / "report.md", row[0])
        atomic_write_text(paths.run_dir(run_id) / "report.json", row[1])
    except OSError:
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, detail, created_at) "
            "VALUES (?, 'report-failure', 'recipe report write failed; SQLite "
            "recovery truth retained', ?)",
            (row_id, _now_utc()),
        )
        conn.commit()
        return {
            "schema_name": "grossy.weekly.recipe-result",
            "schema_version": 1,
            "status": "error",
            "run_id": run_id,
            "report_failed": True,
            "errors": [
                "report write failed; retry identical recipes-apply to regenerate persisted reports"
            ],
        }
    conn.execute(
        "UPDATE weekly_reports SET report_written=1, updated_at=? WHERE run_id=?",
        (_now_utc(), row_id),
    )
    conn.commit()
    return {
        "schema_name": "grossy.weekly.recipe-result",
        "schema_version": 1,
        "status": "success",
        "run_id": run_id,
        "lifecycle": "awaiting-recipe-selection",
        "recipe_slots": data["recipe_slots"],
        "reroll_history": data["reroll_history"],
        "report_failed": False,
        "report_md_path": str(paths.run_dir(run_id) / "report.md"),
        "report_json_path": str(paths.run_dir(run_id) / "report.json"),
    }


def run_recipes_apply(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    _check_database_exists(paths)
    config = _load_config_strict(paths)
    proposal = validate_recipe_options(data)
    if proposal.run_id != run_id:
        raise WeeklyPlanError("recipe-options run ID mismatch")
    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            row = conn.execute(
                "SELECT id, lifecycle, history_sync_status FROM planner_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise WeeklyPlanError("unknown weekly run")
            row_id, lifecycle, history = row
            existing = conn.execute(
                "SELECT proposal_checksum, policy_json FROM run_recipe_plans WHERE run_id=?",
                (row_id,),
            ).fetchone()
            if (
                lifecycle == "awaiting-recipe-selection"
                and existing
                and existing[0] == proposal.checksum
            ):
                # An exact retry regenerates stored reports, never replans or changes slots.
                stored = conn.execute(
                    "SELECT report_json FROM weekly_reports WHERE run_id=?", (row_id,)
                ).fetchone()
                return _write_reports(conn, paths, row_id, run_id, json.loads(stored[0]))
            if (
                conn.execute("SELECT 1 FROM weekly_reports WHERE run_id=?", (row_id,)).fetchone()
                is None
            ):
                raise WeeklyPlanError("immutable cart baseline must be applied before recipes")
            if lifecycle != "recurring-apply" or existing or history not in ("success", "skipped"):
                raise WeeklyPlanError(
                    "recipes-apply requires verified recurring completion "
                    "without an existing recipe plan"
                )
            if not conn.execute(
                "SELECT 1 FROM run_events complete WHERE run_id=? "
                "AND event_type='recurring-complete' AND id > "
                "(SELECT COALESCE(MAX(id),0) FROM run_events "
                "WHERE run_id=? AND event_type='recurring-preflight')",
                (row_id, row_id),
            ).fetchone():
                raise WeeklyPlanError("recurring additions/readback must complete before recipes")
            candidates, exclusions = _candidate_snapshots(conn, row_id, config, proposal)
            now, seed = _now_utc(), derive_seed(run_id)
            conn.execute(
                "INSERT INTO run_recipe_plans (run_id,seed,proposal_checksum,proposal_generated_at,"
                "policy_json,candidates_json,exclusions_json,created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    seed,
                    proposal.checksum,
                    proposal.generated_at,
                    _policy(config),
                    json.dumps(candidates, sort_keys=True),
                    json.dumps(exclusions, sort_keys=True),
                    now,
                ),
            )
            dishes: set[str] = set()
            for name, protein in SLOTS.items():
                slot_seed = derive_seed(run_id, name, 0)
                candidate = weighted_choice(
                    [
                        c
                        for c in candidates
                        if c["protein_class"] == protein and c["dish_id"] not in dishes
                    ],
                    slot_seed,
                )
                reason = (
                    None
                    if candidate
                    else f"no eligible established {protein} recipe remains after "
                    f"policy/linked-dish resolution; use explicit recipes discover --from {protein}"
                )
                conn.execute(
                    "INSERT INTO run_recipe_slots (run_id, slot_name, "
                    "protein_class, recipe_revision_id, eligibility_result, "
                    "empty_reason, seed, current_option_id, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        name,
                        protein,
                        candidate["revision_id"] if candidate else None,
                        "eligible" if candidate else "empty",
                        reason,
                        slot_seed,
                        candidate["row_id"] if candidate else None,
                        now,
                    ),
                )
                if candidate:
                    dishes.add(candidate["dish_id"])
                    _show(conn, row_id, run_id, candidate, now)
            conn.execute(
                "INSERT INTO run_events (run_id, event_type, detail, "
                "created_at) VALUES (?, 'recipe-preview-applied', 'read-only "
                "recurring completion followed by read-only recipe planning', ?)",
                (row_id, now),
            )
            return _save_reports(conn, paths, row_id, run_id, now)
        finally:
            conn.close()


def parse_scope(value: str) -> tuple[str, ...]:
    classes = tuple(PROTEIN_CLASSES) if value == "all" else tuple(value.split(","))
    if (
        not classes
        or len(set(classes)) != len(classes)
        or any(c not in PROTEIN_CLASSES for c in classes)
    ):
        raise WeeklyPlanError(
            "--from requires unique subset of meat,fish,poultry,vegetarian or all"
        )
    return classes


def run_reroll_apply(
    paths: GrossyPaths, run_id: str, slot: str, from_classes: str
) -> dict[str, Any]:
    _check_database_exists(paths)
    config = _load_config_strict(paths)
    classes = parse_scope(from_classes)
    if slot not in SLOTS:
        raise WeeklyPlanError("reroll requires stable slot recipe1..recipe4")
    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            row = conn.execute(
                "SELECT id, lifecycle FROM planner_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or row[1] != "awaiting-recipe-selection":
                raise WeeklyPlanError("reroll requires awaiting-recipe-selection")
            row_id = row[0]
            if conn.execute(
                "SELECT 1 FROM run_recipe_selections WHERE run_id=?", (row_id,)
            ).fetchone():
                raise WeeklyPlanError("persisted dinner selection prevents reroll")
            plan = conn.execute(
                "SELECT candidates_json, policy_json FROM run_recipe_plans WHERE run_id=?",
                (row_id,),
            ).fetchone()
            if plan is None or plan[1] != _policy(config):
                raise WeeklyPlanError(
                    "reroll policy changed or persisted plan missing; renewed review required"
                )
            candidates = json.loads(plan[0])
            seen = {
                r[0]
                for r in conn.execute(
                    "SELECT recipe_id FROM run_recipe_seen WHERE run_id=?", (row_id,)
                )
            }
            seen_dishes = {c["dish_id"] for c in candidates if c["row_id"] in seen}
            eligible = []
            for candidate in candidates:
                if (
                    candidate["row_id"] in seen
                    or candidate["dish_id"] in seen_dishes
                    or candidate["protein_class"] not in classes
                ):
                    continue
                state = conn.execute(
                    "SELECT enabled FROM recipes WHERE id=?", (candidate["row_id"],)
                ).fetchone()
                if state and state[0] and not _eligibility(conn, candidate, config):
                    # Recheck local blacklists without replacing immutable match snapshots.
                    from grossy.history.policy import product_gate

                    if any(
                        (
                            i["exact_match"]
                            and product_gate(conn, i["exact_match"]["product_id"]).is_blocked
                        )
                        or any(
                            product_gate(conn, fallback["product_id"]).is_blocked
                            for fallback in i["approved_family_fallbacks"]
                        )
                        for i in candidate["preview"]["ingredients"]
                    ):
                        continue
                    eligible.append(candidate)
            previous = conn.execute(
                "SELECT current_option_id FROM run_recipe_slots WHERE run_id=? AND slot_name=?",
                (row_id, slot),
            ).fetchone()
            if previous is None:
                raise WeeklyPlanError("persisted stable slot missing")
            counter = conn.execute(
                "SELECT COALESCE(MAX(reroll_number), 0)+1 FROM run_rerolls "
                "WHERE run_id=? AND slot_name=?",
                (row_id, slot),
            ).fetchone()[0]
            seed, now = derive_seed(run_id, slot, counter), _now_utc()
            candidate = weighted_choice(eligible, seed)
            reason = (
                None
                if candidate
                else "no eligible unseen recipe in requested classes; prior option preserved"
            )
            conn.execute(
                "INSERT INTO run_rerolls (run_id, slot_name, reroll_number, "
                "candidate_scope, previous_recipe_id, new_recipe_id, result, "
                "reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    slot,
                    counter,
                    ",".join(classes),
                    previous[0],
                    candidate["row_id"] if candidate else previous[0],
                    "success" if candidate else "no_candidate",
                    reason,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO run_events (run_id, event_type, detail, created_at) "
                "VALUES (?, 'recipe-reroll-preview', ?, ?)",
                (
                    row_id,
                    f"{slot}; reroll={counter}; seed={seed}; scope={','.join(classes)}; "
                    f"result={'success' if candidate else 'no_candidate'}",
                    now,
                ),
            )
            if candidate:
                conn.execute(
                    "UPDATE run_recipe_slots SET protein_class=?, "
                    "recipe_revision_id=?, current_option_id=?, "
                    "eligibility_result='eligible', empty_reason=NULL, seed=? "
                    "WHERE run_id=? AND slot_name=?",
                    (
                        candidate["protein_class"],
                        candidate["revision_id"],
                        candidate["row_id"],
                        seed,
                        row_id,
                        slot,
                    ),
                )
                _show(conn, row_id, run_id, candidate, now)
                conn.execute(
                    "INSERT INTO recipe_events (recipe_id, event_type, run_id, "
                    "detail, created_at) VALUES (?, 'rerolled', ?, ?, ?)",
                    (candidate["row_id"], run_id, f"{slot}; reroll={counter}; seed={seed}", now),
                )
            return _save_reports(conn, paths, row_id, run_id, now)
        finally:
            conn.close()
