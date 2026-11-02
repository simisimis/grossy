"""Private owner-attested delivery-slot barrier; no browser or application state access."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

NAME = "delivery-slot-readiness.json"
SCHEMA = "grossy.live.delivery-slot-readiness.v1"


def load(work: Path) -> dict:
    try:
        state = json.loads((work / NAME).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("delivery_slot_readiness_missing_or_invalid") from exc
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_name", "schema_version", "mode", "status", "confirmed_at"}
        or state["schema_name"] != SCHEMA
        or type(state["schema_version"]) is not int
        or state["schema_version"] != 1
        or state["mode"] not in ("weekly", "history")
        or state["status"] not in ("awaiting-owner", "owner-confirmed", "not-required")
    ):
        raise ValueError("delivery_slot_readiness_missing_or_invalid")
    if state["mode"] == "history":
        valid = state["status"] == "not-required" and state["confirmed_at"] is None
    elif state["status"] == "awaiting-owner":
        valid = state["confirmed_at"] is None
    elif state["status"] == "owner-confirmed":
        try:
            stamp = datetime.fromisoformat(state["confirmed_at"])
            valid = stamp.utcoffset() is not None
        except (TypeError, ValueError):
            valid = False
    else:
        valid = False
    if not valid:
        raise ValueError("delivery_slot_readiness_missing_or_invalid")
    return state


def save(work: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".slot-", dir=work)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, work / NAME)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(work: Path, *, history_only: bool, reuse: bool) -> None:
    if reuse and (work / NAME).exists():
        state = load(work)
        if (state["mode"] == "history") != history_only:
            raise ValueError("delivery_slot_mode_changed_use_new_work_directory")
        return
    if (work / "plan-prepare.json").exists():
        raise ValueError("legacy_run_requires_separate_delivery_slot_review")
    save(work, {
        "schema_name": SCHEMA,
        "schema_version": 1,
        "mode": "history" if history_only else "weekly",
        "status": "not-required" if history_only else "awaiting-owner",
        "confirmed_at": None,
    })


def require_ready(work: Path, *, history: bool = False) -> None:
    state = load(work)
    if history and state["mode"] == "history":
        return
    if state["mode"] != "weekly" or state["status"] != "owner-confirmed":
        raise ValueError("select_delivery_slot_manually_then_report_slot_selected")


def confirm(work: Path) -> None:
    state = load(work)
    if state["mode"] != "weekly":
        raise ValueError("delivery_slot_confirmation_requires_weekly_start")
    if (work / "plan-prepare.json").exists():
        raise ValueError("planning_started_slot_change_requires_separate_review")
    if not (work / "session.txt").is_file() or not (work / "task-tab-target.json").is_file():
        raise ValueError("delivery_slot_browser_start_incomplete")
    if state["status"] == "owner-confirmed":
        return
    state.update(status="owner-confirmed", confirmed_at=datetime.now(timezone.utc).isoformat())
    save(work, state)
