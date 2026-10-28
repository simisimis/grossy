"""Execute the coordinator loop against private SQLite state and fake browser reads."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from test_weekly_mutation import now, observation, url

from grossy.history.maintenance import run_product_register
from grossy.init import do_init
from grossy.paths import resolve_paths
from grossy.weekly.mutation import (
    run_action_check,
    run_recurring_intent,
    run_recurring_observe,
    run_recurring_preflight,
)
from grossy.weekly.plan import run_baseline_apply, run_plan_prepare


@pytest.mark.parametrize("fallback,credit", [(True, 0), (False, 0), (True, 1)])
def test_loop_finishes_after_verified_salad_becomes_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool, credit: int
) -> None:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = [
        {
            "key": "salad",
            "product_id": "111",
            "product_url": url("111"),
            "quantity": 1 + credit,
            "allow_substitution": fallback,
            "approved_alternative_product_ids": ["333"],
        },
        {"key": "cheese", "product_id": "222", "product_url": url("222"), "quantity": 1},
    ]
    paths.config_file.write_text(yaml.safe_dump(config))
    if fallback:
        run_product_register(paths, url("333"))
    plan = run_plan_prepare(paths, skip_history_sync=True)
    run_id = plan["run_id"]
    initial_quantities = {"999": 8, **({"333": credit} if credit else {})}
    baseline = run_baseline_apply(paths, run_id, observation(initial_quantities))
    work = tmp_path / "private-work"
    work.mkdir(mode=0o700)
    titles = {"111": "AB Salad", "222": "AB Cheese", "333": "AB Other Salad", "999": "AB Baseline"}
    quantities = dict(initial_quantities)
    unavailable: set[str] = set()
    clicks: list[str] = []
    page: list[str] = ["about:blank"]  # the single task tab's current location

    def write(name: str, data: Any) -> None:
        (work / name).write_text(json.dumps(data))

    def product(
        pid: str, canonical_url: str, required_quantity: int | None = None, **_: Any
    ) -> dict[str, Any]:
        page[0] = f"/p/{pid}"
        stock = "unavailable" if pid in unavailable else "available"
        return {
            "product_id": pid,
            "canonical_url": canonical_url,
            "title": titles[pid],
            "availability": stock,
            "displayed_price_eur": None if pid in unavailable else "2.00",
            "quantity": 0 if pid in unavailable else quantities.get(pid, 0),
        }

    readings = [
        product(pid, url(pid)) for pid in ("111", "222", "999", *(("333",) if credit else ()))
    ]
    initial = observation(quantities)
    initial["recurring_product_facts"] = [
        {k: r[k] for k in ("product_id", "canonical_url", "availability", "displayed_price_eur")}
        for r in readings
    ]
    preflight = run_recurring_preflight(paths, run_id, initial)
    write("plan-prepare.json", plan)
    write("baseline-result.json", baseline)
    write("baseline-bound.json", {pid: product(pid, url(pid)) for pid in quantities})
    write("recurring-readings.json", readings)
    write("recurring-preflight.json", preflight)
    write("known-urls.json", {pid: url(pid) for pid in titles})
    write("verified-recurring-quantities.json", quantities)

    def grossy(*args: str, out: str | None = None) -> dict[str, Any]:
        handlers = {
            "recurring-preflight": run_recurring_preflight,
            "recurring-intent": run_recurring_intent,
            "recurring-observe": run_recurring_observe,
            "action-check": run_action_check,
        }
        assert args[0] == "weekly"
        result = handlers[args[1]](paths, args[2], json.loads(Path(args[3]).read_text()))
        if out:
            write(out, result)
        if args[1] == "recurring-observe" and result["status"] == "success" and "111" in quantities:
            # Stock changes only after the first addition has a verified readback.
            unavailable.add("111")
            quantities.pop("111")
        return result

    def add_units(pid: str, before: int, delta: int, *_: Any) -> int:
        # live click_once raises unexpected_product unless the tab is on the exact target page
        assert page[0] == f"/p/{pid}", f"click attempted on {page[0]}"
        assert pid not in unavailable
        assert quantities.get(pid, 0) == before
        clicks.append(pid)
        quantities[pid] = before + delta
        return quantities[pid]

    def checkout_rows(*_: Any) -> dict[str, Any]:
        page[0] = "/checkout"
        rows = [
            {
                "name": titles[pid].removeprefix("AB "),
                "brand": "AB",
                "quantity": str(q),
                "price_parts": ["€", "2", "00"],
            }
            for pid, q in quantities.items()
        ]
        rows += [{"name": "Salad", "brand": "AB", "unavailable": True} for _ in unavailable]
        return {"rows": rows, "empty": 0, "badge": [str(sum(quantities.values()))]}

    drill = ModuleType("live_common")
    for key, value in {
        "D": work,
        "now": now,
        "write": write,
        "grossy": grossy,
        "product": product,
        "add_units": add_units,
        "BACKOFF": [0, 0, 0],
    }.items():
        setattr(drill, key, value)

    def stop(reason: str) -> None:
        pytest.fail("unexpected coordinator stop: " + reason)

    drill.stop = stop  # type: ignore[attr-defined]
    binding = ModuleType("cart_bind")
    binding.checkout_rows = checkout_rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "live_common", drill)
    monkeypatch.setitem(sys.modules, "cart_bind", binding)
    monkeypatch.setattr(sys, "argv", ["recurring_add.py", "20"])
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / ".pi/skills/grossy/scripts")
    )
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / ".pi/skills/grossy/scripts/recurring_add.py")
    )
    assert clicks == (["111", "333", "222"] if fallback else ["111", "222"])
    assert quantities == (
        {"999": 8, "333": 1 + credit, "222": 1} if fallback else {"999": 8, "222": 1}
    )
    assert json.loads((work / "verified-recurring-quantities.json").read_text()) == quantities
    report = json.loads((paths.run_dir(run_id) / "report.json").read_text())
    assert report["lifecycle"] == "awaiting-basket-review"
    assert report["recurring_unavailable_product_ids"] == ["111"]
    manifests = list(work.glob("action-*-manifest-recurring-intent-result.json"))
    assert len(manifests) == len(clicks) + 1  # each write plus zero-addition completion
