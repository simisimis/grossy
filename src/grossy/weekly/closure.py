"""State-oriented closure and mechanical SQLite report recovery; no browser actions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.paths import GrossyPaths
from grossy.weekly.mutation import _event, _pending
from grossy.weekly.plan import WeeklyPlanError, _check_database_exists
from grossy.weekly.report import build_json_report, build_markdown_report


@contextmanager
def _state_locked(paths: GrossyPaths) -> Iterator[sqlite3.Connection]:
    # Closure/recovery remain usable even when YAML policy itself needs repair.
    _check_database_exists(paths)
    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def write_report(conn: sqlite3.Connection, paths: GrossyPaths, rid: int, run_id: str) -> None:
    row = conn.execute(
        "SELECT report_json,report_md FROM weekly_reports WHERE run_id=?", (rid,)
    ).fetchone()
    if not row:
        raise WeeklyPlanError("no persisted report to regenerate")
    conn.execute("UPDATE weekly_reports SET report_written=0 WHERE run_id=?", (rid,))
    conn.commit()
    try:
        directory = paths.runs_dir / run_id
        ensure_private_dir(directory)
        atomic_write_text(directory / "report.json", build_json_report(json.loads(row[0])))
        atomic_write_text(directory / "report.md", row[1])
    except OSError:
        _event(conn, rid, "report-failure", {"status": "report-recovery-required"})
        conn.commit()
        raise
    conn.execute("UPDATE weekly_reports SET report_written=1 WHERE run_id=?", (rid,))


def persist_report(
    conn: sqlite3.Connection,
    paths: GrossyPaths,
    rid: int,
    run_id: str,
    lifecycle: str,
    evidence: dict[str, Any],
) -> None:
    from grossy.weekly.plan import _get_source_order_ids
    from grossy.weekly.recipes import _sanitize_tree

    row = conn.execute("SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)).fetchone()
    if not row:
        conn.commit()  # abandonment before baseline still persists closure
        return
    data = json.loads(row[0])
    data["lifecycle"] = lifecycle
    data.setdefault("removal_closure_evidence", []).append(evidence)
    observed = evidence.get("observation")
    if observed and observed.get("mutation_result", {}).get("outcome") == "readback":
        total = sum(
            Decimal(line["displayed_price_eur"]) * line["quantity"] for line in observed["lines"]
        )
        budget = data["budget"]
        projected = total + sum(
            Decimal(budget.get(key) or "0")
            for key in ("projected_recipe_eur", "projected_recurring_eur")
        )
        budget.update(
            resulting_total_eur=f"{total:.2f}",
            projected_total_eur=f"{projected:.2f}",
            variance_eur=f"{projected - Decimal(budget['weekly_budget_eur']):.2f}",
        )

    data = _sanitize_tree(data, _get_source_order_ids(conn))
    conn.execute(
        "UPDATE weekly_reports SET report_json=?,report_md=?,report_written=0 WHERE run_id=?",
        (json.dumps(data), build_markdown_report(data), rid),
    )
    write_report(conn, paths, rid, run_id)


def run_report_regenerate(paths: GrossyPaths, run_id: str) -> dict[str, Any]:
    with _state_locked(paths) as conn:
        row = conn.execute("SELECT id FROM planner_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise WeeklyPlanError("unknown run")
        write_report(conn, paths, int(row[0]), run_id)
        return {
            "schema_name": "grossy.weekly.report-regenerate.v1",
            "schema_version": 1,
            "status": "success",
        }


def run_close(
    paths: GrossyPaths, run_id: str, *, abandon: bool = False, no_recipes: bool = False
) -> dict[str, Any]:
    with _state_locked(paths) as conn:
        row = conn.execute(
            "SELECT id,lifecycle FROM planner_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row or row[1] in ("completed", "abandoned"):
            raise WeeklyPlanError("closure requires an active run")
        rid = int(row[0])
        if not abandon:
            if any(_pending(conn, rid, stage) for stage in ("recurring", "recipe", "removal")):
                raise WeeklyPlanError(
                    "unreconciled intent prevents finish; abandon preserves evidence"
                )
            required = "awaiting-recipe-selection" if no_recipes else "awaiting-user-verification"
            if row[1] != required:
                raise WeeklyPlanError(
                    "finish requires reviewed cart; --no-recipes requires awaiting-recipe-selection"
                )
            if (
                no_recipes
                and conn.execute(
                    "SELECT 1 FROM run_recipe_selections WHERE run_id=?", (rid,)
                ).fetchone()
            ):
                raise WeeklyPlanError("cannot discard persisted recipe selection")
        lifecycle = "abandoned" if abandon else "completed"
        evidence = {
            "status": lifecycle,
            "cart_review_only": not abandon,
            "no_recipes": no_recipes,
            "cart_removed": False,
            "order_placed": False,
        }
        if no_recipes:
            _event(conn, rid, "selection-empty", {"deliberate": True, "selected_slots": []})
        conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
        _event(conn, rid, "run-closure", evidence)
        persist_report(conn, paths, rid, run_id, lifecycle, evidence)
        return {
            "schema_name": "grossy.weekly.closure.v1",
            "schema_version": 1,
            "status": "success",
            "lifecycle": lifecycle,
            "order_placed": False,
        }
