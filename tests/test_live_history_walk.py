"""Offline fake-browser tests for the reservation-history enumeration walk in history_sync.py.

The real live_common module runs against a fake page (evaluate/browser/settle_page seams) and
a fake clock; CLI phases run in-process against a private temporary home. No agent-browser,
no network, no real state.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_history_sync import _seed_imported

from grossy.history import (
    run_discover_prepare,
    run_scan_prepare,
    run_sync_dry_run,
    validate_listed_orders,
)
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/grossy/scripts"
LIST_URL = "https://www.ab.gr/en/my-account/reservation-history"


def order_id(index: int) -> str:
    return f"{900 - index:09d}"  # newest first, nine digits, leading zeros


class Stop(Exception):
    pass


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class FakeList:
    """Reservation-history list rendering 10 entries, then 10 more per settled bottom pass.

    ``stall``: the bottom is never reached (content keeps the page longer than the viewport).
    ``busy``: the network never idles, so no pass settles. Every seam records the ``recover``
    flag it was called with and the settle timeouts it was given."""

    def __init__(self, dates: list[str], *, stall: bool = False, busy: bool = False) -> None:
        self.entries = [(order_id(i), date) for i, date in enumerate(dates)]
        self.rendered = min(10, len(self.entries))
        self.stall = stall
        self.busy = busy
        self.opened: list[str] = []
        self.scrolls = 0
        self.at_bottom = False
        self.scrolled = False  # lazy loading is triggered by a scroll and lands on settle
        self.recover_flags: list[bool] = []
        self.settle_timeouts: list[float] = []

    def browser(self, args: list[str], tag: str = "cmd", recover: bool = True) -> str:
        self.recover_flags.append(recover)
        if args[0] == "open":
            self.opened.append(args[1])
        return ""

    def settle_page(self, tag: str = "settle", recover: bool = True, timeout: float = 40) -> bool:
        self.recover_flags.append(recover)
        self.settle_timeouts.append(timeout)
        if self.busy:
            return False
        if self.scrolled and self.rendered < len(self.entries):
            self.rendered = min(len(self.entries), self.rendered + 10)
            self.at_bottom = False  # the page grew below the scroll position
        self.scrolled = False
        return True

    def evaluate(self, body: str, tag: str = "eval", recover: bool = True) -> Any:
        import live_common

        self.recover_flags.append(recover)
        if body == live_common.SCROLL_BOTTOM_JS:
            self.scrolls += 1
            self.scrolled = True
            self.at_bottom = not self.stall
            return True
        if body == live_common.AT_BOTTOM_JS:
            return self.at_bottom
        if "order-details-button" in body:
            return [
                {"hrefs": [f"/my-account/order?id={sid}"], "dates": [date]}
                for sid, date in self.entries[: self.rendered]
            ]
        if 'readyState==="complete"' in body or body.endswith("return true;"):
            return True
        pytest.fail("unexpected page read: " + body[:80])


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    return paths


def run_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: GrossyPaths,
    page: FakeList,
    *args: str,
) -> tuple[Path, Any, FakeClock]:
    """Run history_sync.py against ``page``; return (work dir, exit outcome, clock)."""
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setenv("GROSSY_LIVE_DIR", str(work))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    gate = runpy.run_path(str(SCRIPTS / "slot_gate.py"))
    gate["initialize"](work, history_only=True, reuse=False)
    spec = importlib.util.spec_from_file_location("live_common", SCRIPTS / "live_common.py")
    assert spec is not None and spec.loader is not None
    drill = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    spec.loader.exec_module(drill)
    clock = FakeClock()

    def stop(reason: str) -> None:
        raise Stop(reason)

    def grossy(*args: str, out: str | None = None, **_: Any) -> dict[str, Any]:
        assert args[0] == "history"
        if args[1] == "discover-prepare":
            result = run_discover_prepare(paths).to_dict()
        else:
            assert args[2] == "--listed-orders"
            phase = {"sync-dry-run": run_sync_dry_run, "scan-prepare": run_scan_prepare}[args[1]]
            result = phase(Path(args[3]), paths).to_dict()
        if out:
            drill.write(out, result)
        return result

    for key, value in {
        "time": clock,
        "stop": stop,
        "grossy": grossy,
        "browser": page.browser,
        "evaluate": page.evaluate,
        "settle_page": page.settle_page,
    }.items():
        monkeypatch.setattr(drill, key, value)
    monkeypatch.setattr(sys, "argv", ["history_sync.py", *args])
    try:
        runpy.run_path(str(SCRIPTS / "history_sync.py"))
    except (SystemExit, Stop) as exc:
        outcome: Any = exc
    else:
        outcome = None
    assert page.opened == [LIST_URL]
    # Strict pinned tab on every browser command of the history walk (no recovery path).
    assert page.recover_flags and not any(page.recover_flags)
    assert all(timeout <= drill.STALL_SECONDS for timeout in page.settle_timeouts)
    return work, outcome, clock


def listed(work: Path) -> list[dict[str, str]]:
    data = json.loads((work / "listed-orders.json").read_text())
    orders = validate_listed_orders(data).orders
    return [o.model_dump() for o in orders]


def enumeration(work: Path) -> dict[str, Any]:
    return json.loads((work / "enumeration.json").read_text())


def test_list_without_entries_stops_as_missing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    # No verified empty-list state exists: zero rendered entries is missing identity, not an
    # empty history. Nothing is walked and no proposal or enumeration result is written.
    page = FakeList([])
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, "--dry-run")
    assert isinstance(outcome, Stop) and str(outcome) == "list_identity_missing"
    assert page.scrolls == 0
    assert (work / "discover-prepare.json").exists()
    assert not (work / "listed-orders.json").exists()
    assert not (work / "enumeration.json").exists()
    assert not (work / "dry-run.json").exists()


@pytest.mark.parametrize("count,passes", [(1, 2), (25, 4)])
def test_walk_lists_every_order_newest_first_after_two_settled_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths, count: int, passes: int
) -> None:
    page = FakeList([f"{1 + i % 28:02d}/10/2026" for i in range(count)])
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, "--dry-run")
    assert isinstance(outcome, SystemExit) and outcome.code == 0
    orders = listed(work)
    assert [o["source_order_id"] for o in orders] == [order_id(i) for i in range(count)]
    assert enumeration(work) == {
        "status": "complete",
        "reason": "settled",
        "mode": "default",
        "ended": "settled",
        "bottom_passes": passes,
        "listed_order_count": count,
        "known_order_count": 0,
        "last_known_order_id": None,
        "last_imported_order_id": None,
    }
    assert json.loads((work / "dry-run.json").read_text())["unknown_order_count"] == count
    assert not (work / "scan-prepare.json").exists()


def test_leading_zero_ids_and_day_first_dates_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["05/11/2026"])
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and "per_order_scan_not_available" in str(outcome)
    assert listed(work) == [
        {
            "source_order_id": "000000900",
            "order_date": "2026-11-05",
            "navigation_url": "https://www.ab.gr/my-account/order?id=000000900",
        }
    ]
    prepared = json.loads((work / "scan-prepare.json").read_text())
    assert prepared["status"] == "ready" and prepared["unknown_order_count"] == 1
    assert prepared["unknown_orders"][0]["source_order_id"] == "000000900"


def test_default_walk_stops_at_first_imported_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    _seed_imported(home.database_file, [order_id(i) for i in range(12, 25)])
    page = FakeList(["01/10/2026"] * 25)
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and "per_order_scan_not_available" in str(outcome)
    assert [o["source_order_id"] for o in listed(work)] == [order_id(i) for i in range(13)]
    result = enumeration(work)
    assert result["status"] == "complete"
    assert result["ended"] == result["reason"] == "stopped_at_known_order"
    assert result["bottom_passes"] == 1 and page.scrolls == 1
    assert result["known_order_count"] == 1
    assert result["last_known_order_id"] == order_id(12)
    prepared = json.loads((work / "scan-prepare.json").read_text())
    assert prepared["unknown_order_count"] == 12
    assert prepared["unknown_orders"][0]["source_order_id"] == order_id(11)  # oldest first


def test_full_walk_skips_imported_orders_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    _seed_imported(home.database_file, [order_id(i) for i in (3, *range(12, 25))])
    page = FakeList(["01/10/2026"] * 25)
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, "--full", "--dry-run")
    assert isinstance(outcome, SystemExit) and outcome.code == 0
    assert [o["source_order_id"] for o in listed(work)] == [order_id(i) for i in range(25)]
    result = enumeration(work)
    assert result["mode"] == "full" and result["ended"] == "settled"
    assert result["bottom_passes"] == 4
    assert result["known_order_count"] == 14
    assert result["last_known_order_id"] == order_id(24)
    assert json.loads((work / "dry-run.json").read_text())["unknown_order_count"] == 11


def test_stall_rule_ends_the_run_with_a_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["01/10/2026"] * 10, stall=True)
    work, outcome, clock = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome).startswith("enumeration_stalled")
    assert 30 <= clock.now < 32
    assert page.scrolls == 20  # no scroll-count ceiling: only the 30 s stall rule ended it
    result = enumeration(work)
    assert result["status"] == "partial" and result["reason"] == "stall_rule"
    assert result["ended"] == "stalled" and result["listed_order_count"] == 10
    assert result["last_imported_order_id"] is None
    assert len(listed(work)) == 10
    assert not (work / "dry-run.json").exists()


def test_busy_network_never_counts_as_settled_and_ends_as_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["01/10/2026"] * 10, busy=True)
    work, outcome, clock = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome).startswith("enumeration_stalled")
    assert 30 <= clock.now < 32
    assert page.scrolls == 20
    result = enumeration(work)
    assert result["status"] == "partial" and result["reason"] == "stall_rule"
    assert result["listed_order_count"] == 10 and result["last_imported_order_id"] is None
    # Each network wait is bounded by the remaining stall budget, never the old fixed 40 s.
    walk_timeouts = page.settle_timeouts[1:]
    assert walk_timeouts[0] == 30 and walk_timeouts[-1] >= 1
    assert walk_timeouts == sorted(walk_timeouts, reverse=True) and len(set(walk_timeouts)) == 20


def test_malformed_entry_stops_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["2026-10-01"])
    _, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome) == "order_entry_malformed"


def load_live_common(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("GROSSY_LIVE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("live_common", SCRIPTS / "live_common.py")
    assert spec is not None and spec.loader is not None
    drill = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    spec.loader.exec_module(drill)

    def stop(reason: str) -> None:
        raise Stop(reason)

    monkeypatch.setattr(drill, "stop", stop)
    return drill


class FakeAgentBrowser:
    """subprocess.run stand-in: the pinned target cannot be selected; everything else succeeds
    (blank tabs only, so the default recovery path would relaunch)."""

    def __init__(self, *, network_busy: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.network_busy = network_busy

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["agent-browser", "--session"]
        self.calls.append(args)
        self.timeouts.append(kwargs.get("timeout"))
        command = [a for a in args[3:] if not a.startswith("--")]
        if command[:2] == ["tab", "list"]:
            tabs = [{"targetId": "relaunched", "url": "about:blank"}]
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"tabs": tabs}}), "")
        if command[:1] == ["tab"]:
            return subprocess.CompletedProcess(args, 1, "", "tab_gone")
        if command[:1] == ["wait"] and self.network_busy:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, "true", "")

    def commands(self) -> list[str]:
        """The agent-browser subcommand of every call (tab, open, close, wait, eval ...)."""
        return [c[c.index("--pin-tab") + 1] if "--pin-tab" in c else c[3] for c in self.calls]


@pytest.mark.parametrize("seam", ["browser", "evaluate", "settle_page", "bottom_pass"])
def test_strict_pinned_tab_stops_on_target_loss_without_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    drill = load_live_common(tmp_path, monkeypatch)
    target = json.dumps({"target_id": "original"})
    (tmp_path / "task-tab-target.json").write_text(target)
    fake = FakeAgentBrowser()
    monkeypatch.setattr(drill.subprocess, "run", fake.run)
    monkeypatch.setattr(drill.time, "sleep", lambda seconds: None)
    call = {
        "browser": lambda: drill.browser(["open", LIST_URL], "t", recover=False),
        "evaluate": lambda: drill.evaluate("return true;", "t", recover=False),
        "settle_page": lambda: drill.settle_page("t", recover=False),
        "bottom_pass": lambda: drill.bottom_pass("t"),
    }[seam]
    with pytest.raises(Stop, match="^exact_task_tab_unavailable$"):
        call()
    # Exactly one pinned-target selection, then nothing: no tab list, close, relaunch, retry,
    # wait or eval; the bound target is unchanged.
    assert len(fake.calls) == 1 and fake.commands() == ["tab"]
    assert fake.calls[0][-1] == "original" and "--pin-tab" in fake.calls[0]
    assert (tmp_path / "task-tab-target.json").read_text() == target


def test_default_tab_selection_recovers_only_with_recover_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = load_live_common(tmp_path, monkeypatch)
    (tmp_path / "task-tab-target.json").write_text(json.dumps({"target_id": "original"}))
    fake = FakeAgentBrowser()
    monkeypatch.setattr(drill.subprocess, "run", fake.run)
    with pytest.raises(Stop, match="^exact_task_tab_unavailable$"):
        drill.settle_page("t")
    # The unrelated callers' path consulted the tab list and relaunched before giving up.
    assert ["tab", "list"] in [c[3:5] for c in fake.calls]
    assert "open" in fake.commands()


def test_settle_page_reports_a_busy_network_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = load_live_common(tmp_path, monkeypatch)
    (tmp_path / "task-tab-target.json").write_text(json.dumps({"target_id": "original"}))
    fake = FakeAgentBrowser(network_busy=True)
    monkeypatch.setattr(drill, "select_task_tab", lambda recover=True: None)
    monkeypatch.setattr(drill.subprocess, "run", fake.run)
    assert drill.settle_page("t", recover=False, timeout=7.5) is False
    assert fake.commands() == ["wait"] and fake.timeouts == [7.5]
    assert fake.calls[0][-3:] == ["wait", "--load", "networkidle"] and "--pin-tab" in fake.calls[0]


def test_tab_list_failure_means_no_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drill = load_live_common(tmp_path, monkeypatch)

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["agent-browser", "--session"] and args[3:] == ["tab", "list", "--json"]
        body = {"success": False, "data": None, "error": "Auto-launch failed"}
        return subprocess.CompletedProcess(args, 1, json.dumps(body), "")

    monkeypatch.setattr(drill.subprocess, "run", fake_run)
    assert drill._tabs() is None


def test_start_opens_reservation_history_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "grossy").mkdir()
    opened: list[str] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0] == "agent-browser"
        if "list" in args:
            tabs = [{"targetId": "fake-target", "url": "about:blank"}]
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"tabs": tabs}}), "")
        if "open" in args:
            opened.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["live_start.py", "--history-only"])
    monkeypatch.syspath_prepend(str(SCRIPTS))
    runpy.run_path(str(SCRIPTS / "live_start.py"))
    assert opened == [LIST_URL]
