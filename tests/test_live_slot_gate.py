"""Offline manual delivery-slot handoff tests; never open a real browser/profile."""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/grossy/scripts"
spec = importlib.util.spec_from_file_location("slot_gate", SCRIPTS / "slot_gate.py")
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def browser_started(work: Path) -> None:
    (work / "session.txt").write_text("fake-task-session")
    (work / "task-tab-target.json").write_text(json.dumps({"target_id": "fake-target"}))


def test_confirmation_is_private_work_scoped_and_survives_rebind(tmp_path: Path) -> None:
    gate.initialize(tmp_path, history_only=False, reuse=False)
    with pytest.raises(ValueError, match="select_delivery_slot_manually"):
        gate.require_ready(tmp_path)
    browser_started(tmp_path)
    gate.confirm(tmp_path)
    before = gate.load(tmp_path)
    gate.confirm(tmp_path)  # repeated owner handoff before planning is harmless
    gate.initialize(tmp_path, history_only=False, reuse=True)
    assert gate.load(tmp_path) == before
    gate.require_ready(tmp_path)
    gate.require_ready(tmp_path, history=True)
    assert (tmp_path / gate.NAME).stat().st_mode & 0o777 == 0o600
    other = tmp_path / "next-week"
    other.mkdir()
    gate.initialize(other, history_only=False, reuse=False)
    with pytest.raises(ValueError, match="select_delivery_slot_manually"):
        gate.require_ready(other)


def test_history_only_cannot_authorize_weekly_prefill(tmp_path: Path) -> None:
    gate.initialize(tmp_path, history_only=True, reuse=False)
    gate.require_ready(tmp_path, history=True)
    with pytest.raises(ValueError, match="select_delivery_slot_manually"):
        gate.require_ready(tmp_path)
    with pytest.raises(ValueError, match="requires_weekly_start"):
        gate.confirm(tmp_path)
    with pytest.raises(ValueError, match="mode_changed"):
        gate.initialize(tmp_path, history_only=False, reuse=True)


def test_confirmation_requires_completed_start_and_cannot_change_active_run(tmp_path: Path) -> None:
    gate.initialize(tmp_path, history_only=False, reuse=False)
    with pytest.raises(ValueError, match="browser_start_incomplete"):
        gate.confirm(tmp_path)
    browser_started(tmp_path)
    (tmp_path / "plan-prepare.json").write_text("{}")
    with pytest.raises(ValueError, match="planning_started"):
        gate.confirm(tmp_path)
    (tmp_path / gate.NAME).unlink()
    with pytest.raises(ValueError, match="legacy_run_requires_separate"):
        gate.initialize(tmp_path, history_only=False, reuse=True)


@pytest.mark.parametrize("contents", [None, "not-json", "[]", "{}", '{"schema_version": true}'])
def test_missing_or_malformed_readiness_fails_closed(tmp_path: Path, contents: str | None) -> None:
    if contents is not None:
        (tmp_path / gate.NAME).write_text(contents)
    with pytest.raises(ValueError, match="readiness_missing_or_invalid"):
        gate.require_ready(tmp_path)


@pytest.mark.parametrize("field,value", [("confirmed_at", "no-zone"), ("schema_version", True)])
def test_corrupt_confirmed_state_fails_closed(tmp_path: Path, field: str, value: Any) -> None:
    gate.initialize(tmp_path, history_only=False, reuse=False)
    browser_started(tmp_path)
    gate.confirm(tmp_path)
    state = gate.load(tmp_path)
    state[field] = value
    gate.save(tmp_path, state)
    with pytest.raises(ValueError, match="readiness_missing_or_invalid"):
        gate.require_ready(tmp_path)


@pytest.mark.parametrize(
    "script,mode",
    [
        (script, mode)
        for script in (
            "history_sync",
            "plan_stage1",
            "recurring_add",
            "recipe_add",
            "advance",
            "recipes_apply",
            "matches_apply",
        )
        for mode in ("pending", "history-only", "legacy")
        if not (mode == "history-only" and script == "history_sync")
    ],
)
def test_unready_scripts_stop_before_any_phase_or_browser_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, mode: str
) -> None:
    if mode == "legacy":
        (tmp_path / "plan-prepare.json").write_text('{"run_id": "legacy-run"}')
    else:
        gate.initialize(tmp_path, history_only=mode == "history-only", reuse=False)
    drill = ModuleType("live_common")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a phase, browser call or artifact write ran before slot readiness")

    def stop(reason: str) -> None:
        raise ValueError(reason)

    for key, value in {"D": tmp_path, "stop": stop, "write": forbidden}.items():
        setattr(drill, key, value)
    drill.__getattr__ = lambda name: forbidden  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "live_common", drill)
    monkeypatch.setitem(sys.modules, "cart_bind", ModuleType("cart_bind"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setattr(sys, "argv", [f"{script}.py"])
    reason = "readiness_missing_or_invalid" if mode == "legacy" else "select_delivery_slot_manually"
    with pytest.raises(ValueError, match=reason):
        runpy.run_path(str(SCRIPTS / f"{script}.py"))


@pytest.mark.parametrize("history_only", [False, True])
def test_start_opens_browser_without_history_or_prefill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    history_only: bool,
) -> None:
    import subprocess

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "grossy").mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert args[0] == "agent-browser"
        if "list" in args:
            tabs = [{"targetId": "fake-target", "url": "about:blank"}]
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"tabs": tabs}}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv", ["live_start.py", *(["--history-only"] if history_only else [])]
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    runpy.run_path(str(SCRIPTS / "live_start.py"))
    work = Path((tmp_path / "grossy/live-current.txt").read_text().strip())
    assert gate.load(work)["status"] == ("not-required" if history_only else "awaiting-owner")
    assert ("PAUSE:" in capsys.readouterr().out) is not history_only
    opened = [call for call in calls if "open" in call]
    assert len(opened) == 1
    assert "--headed" in opened[0] and "--profile" in opened[0] and "--pin-tab" in opened[0]
    assert not (work / "plan-prepare.json").exists()


@pytest.mark.parametrize("history_only", [False, True])
def test_history_phase_starts_only_after_applicable_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history_only: bool
) -> None:
    gate.initialize(tmp_path, history_only=history_only, reuse=False)
    if not history_only:
        browser_started(tmp_path)
        gate.confirm(tmp_path)
    drill = ModuleType("live_common")
    calls: list[tuple[str, ...]] = []

    def phase(*args: str, **kwargs: Any) -> None:
        calls.append(args)
        raise RuntimeError("reached-first-phase")

    for key, value in {
        "D": tmp_path,
        "stop": pytest.fail,
        "write": pytest.fail,
        "grossy": phase,
    }.items():
        setattr(drill, key, value)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setattr(sys, "argv", ["history_sync.py"])
    with pytest.raises(RuntimeError, match="reached-first-phase"):
        runpy.run_path(str(SCRIPTS / "history_sync.py"))
    assert calls == [("history", "discover-prepare")]


def test_slot_ready_script_only_records_owner_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate.initialize(tmp_path, history_only=False, reuse=False)
    browser_started(tmp_path)
    drill = ModuleType("live_common")
    drill.D = tmp_path  # type: ignore[attr-defined]
    drill.stop = pytest.fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "live_common", drill)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setattr(sys, "argv", ["slot_ready.py"])
    runpy.run_path(str(SCRIPTS / "slot_ready.py"))
    gate.require_ready(tmp_path)
    assert "no rendered slot verification" in capsys.readouterr().out
