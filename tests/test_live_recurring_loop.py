"""Execute the coordinator loop against private SQLite state and fake browser reads."""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
import time
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

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/grossy/scripts"


def install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    drill_attrs: dict[str, Any],
    argv: list[str],
    checkout_rows: Any = None,
) -> None:
    """Install this test's fake ``live_common``, a fresh real ``cart_bind`` bound to it (its
    checkout read replaced by ``checkout_rows`` when given) and the owner-confirmed slot
    receipt that precedes planning in real workflows, without any delivery-slot details."""
    drill = ModuleType("live_common")
    for key, value in {
        "D": work,
        "now": now,
        "BACKOFF": [0, 0, 0],
        "CHECKOUT_URL": "https://store.example/checkout",
        "STORE": "https://store.example",
        "EMPTY_RE": "/^$/",
        "browser": lambda *_: None,
        "price_of": lambda parts: parts[1] + "." + parts[2],
        **drill_attrs,
    }.items():
        setattr(drill, key, value)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("cart_bind", SCRIPTS / "cart_bind.py")
    assert spec is not None and spec.loader is not None
    binding = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binding)
    if checkout_rows is not None:
        binding.checkout_rows = checkout_rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cart_bind", binding)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    slot_gate = runpy.run_path(str(SCRIPTS / "slot_gate.py"))
    slot_gate["save"](
        work,
        {
            "schema_name": slot_gate["SCHEMA"],
            "schema_version": 1,
            "mode": "weekly",
            "status": "owner-confirmed",
            "confirmed_at": now(),
        },
    )


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

    def stop(reason: str) -> None:
        pytest.fail("unexpected coordinator stop: " + reason)

    fakes = {"write": write, "grossy": grossy, "product": product, "add_units": add_units}
    install_fakes(
        monkeypatch, work, {**fakes, "stop": stop}, ["recurring_add.py", "20"], checkout_rows
    )
    runpy.run_path(str(SCRIPTS / "recurring_add.py"))
    if credit:
        # The approved alternative already in the basket covers the salad: no salad
        # write, so it never becomes unavailable and only the cheese is added.
        assert clicks == ["222"]
        assert quantities == {"999": 8, "333": credit, "222": 1}
    else:
        assert clicks == (["111", "333", "222"] if fallback else ["111", "222"])
        assert quantities == ({"999": 8, "333": 1, "222": 1} if fallback else {"999": 8, "222": 1})
    assert json.loads((work / "verified-recurring-quantities.json").read_text()) == quantities
    report = json.loads((paths.run_dir(run_id) / "report.json").read_text())
    assert report["lifecycle"] == "awaiting-basket-review"
    assert report["recurring_unavailable_product_ids"] == ([] if credit else ["111"])
    if credit:
        assert {"product_id": "111", "status": "cart-covered"} in report[
            "recurring_preflight_decisions"
        ]
    manifests = list(work.glob("action-*-manifest-recurring-intent-result.json"))
    assert len(manifests) == len(clicks) + 1  # each write plus zero-addition completion


@pytest.mark.parametrize("entry", ["loop", "seed"])
@pytest.mark.parametrize("outcome", ["unique", "ambiguous"])
def test_loop_binds_planned_product_already_in_basket_by_page_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, entry: str
) -> None:
    """A checkout row the baseline could not bind turns out to be the planned product.

    The product page shows a positive in-cart quantity, so the loop binds the unique
    quantity-and-price-matching unbound row to it, moves the row out of the unbound record,
    and re-runs preflight: the CLI credits the line and reports the product covered. No
    click, no action-check, no top-up. With two matching rows the loop stops fail-closed
    before intent and leaves every record as read. ``seed`` enters through the first-pass
    whole-cart read (real ``cart_bind`` over the fake checkout), where the known cheese page
    binds the recorded row; ``loop`` starts from recorded verified quantities.
    """
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "cheese", "product_id": "222", "product_url": url("222"), "quantity": 2}
    ]
    paths.config_file.write_text(yaml.safe_dump(config))
    plan = run_plan_prepare(paths, skip_history_sync=True)
    run_id = plan["run_id"]
    bound_quantities = {"999": 8}  # the cheese row bound to no product page at baseline
    baseline = run_baseline_apply(paths, run_id, observation(bound_quantities))
    work = tmp_path / "private-work"
    work.mkdir(mode=0o700)
    titles = {"222": "AB Cheese", "999": "AB Baseline"}
    quantities = {"999": 8, "222": 1}  # what the store basket really holds
    unbound_rows = [{"name": "Cheese", "brand": "AB", "quantity": "1", "price": "2.00"}]
    if outcome == "ambiguous":
        unbound_rows.append({"name": "Butter", "brand": "AB", "quantity": "1", "price": "2.00"})
    clicks: list[str] = []
    checks: list[str] = []
    stops: list[str] = []

    def write(name: str, data: Any) -> None:
        (work / name).write_text(json.dumps(data))

    def product(
        pid: str, canonical_url: str, required_quantity: int | None = None, **_: Any
    ) -> dict[str, Any]:
        # live_common.product waits for the empty purchase panel when 0 is required and for the
        # exact quantity when a positive one is; neither settles on a different cart state.
        if required_quantity is not None and required_quantity != quantities.get(pid, 0):
            pytest.fail(f"quantity gate {required_quantity} would never settle for {pid}")
        return {
            "product_id": pid,
            "canonical_url": canonical_url,
            "title": titles[pid],
            "availability": "available",
            "displayed_price_eur": "2.00",
            "quantity": quantities.get(pid, 0),
        }

    readings = [product(pid, url(pid)) for pid in ("222", "999")]
    initial = observation(bound_quantities)
    initial["recurring_product_facts"] = [
        {k: r[k] for k in ("product_id", "canonical_url", "availability", "displayed_price_eur")}
        for r in readings
    ]
    preflight = run_recurring_preflight(paths, run_id, initial)
    assert [a["product_id"] for a in preflight["manifest"]["additions"]] == ["222"]
    write("plan-prepare.json", plan)
    write("baseline-result.json", baseline)
    write("baseline-bound.json", {"999": product("999", url("999"))})
    write("recurring-readings.json", readings)
    write("recurring-preflight.json", preflight)
    write("known-urls.json", {pid: url(pid) for pid in titles})
    if entry == "loop":
        write("verified-recurring-quantities.json", bound_quantities)
    write("unbound-rows.json", unbound_rows)

    def grossy(*args: str, out: str | None = None) -> dict[str, Any]:
        handlers = {
            "recurring-preflight": run_recurring_preflight,
            "recurring-intent": run_recurring_intent,
            "recurring-observe": run_recurring_observe,
            "action-check": run_action_check,
        }
        assert args[0] == "weekly"
        if args[1] == "action-check":
            checks.append(json.loads(Path(args[3]).read_text())["additions"][0]["product_id"])
        result = handlers[args[1]](paths, args[2], json.loads(Path(args[3]).read_text()))
        if out:
            write(out, result)
        return result

    def add_units(pid: str, before: int, delta: int, *_: Any) -> int:
        clicks.append(pid)
        quantities[pid] = before + delta
        return quantities[pid]

    def checkout_rows(*_: Any) -> dict[str, Any]:
        rows = [
            {
                "name": titles[pid].removeprefix("AB "),
                "brand": "AB",
                "quantity": str(q),
                "price_parts": ["€", "2", "00"],
            }
            for pid, q in quantities.items()
        ]
        rows += [
            {
                "name": u["name"],
                "brand": u["brand"],
                "quantity": "1",
                "price_parts": ["€", "2", "00"],
            }
            for u in unbound_rows[1:]
        ]
        return {
            "rows": rows,
            "empty": 0,
            "badge": [str(sum(quantities.values()) + len(unbound_rows) - 1)],
        }

    def stop(reason: str) -> None:
        stops.append(reason)
        raise SystemExit(1)

    def evaluate(script: str, tag: str | None = None) -> Any:
        # The settled /checkout read for the real cart_bind; the exact-search page has no cards.
        if "unexpected_cart_route" in script:
            return {"ready": "complete", **checkout_rows()}
        return "settled" if 'readyState==="complete"?' in script else []

    fakes = {
        "write": write,
        "grossy": grossy,
        "product": product,
        "add_units": add_units,
        "stop": stop,
        "evaluate": evaluate,
    }
    install_fakes(
        monkeypatch,
        work,
        fakes,
        ["recurring_add.py", "5"],
        checkout_rows if entry == "loop" else None,
    )
    if outcome == "unique":
        runpy.run_path(str(SCRIPTS / "recurring_add.py"))
    else:
        with pytest.raises(SystemExit):
            runpy.run_path(str(SCRIPTS / "recurring_add.py"))
    assert clicks == [] and checks == []
    assert quantities == {"999": 8, "222": 1}
    report = json.loads((paths.run_dir(run_id) / "report.json").read_text())
    tag = "seed" if entry == "seed" else "action-01"
    if outcome == "unique":
        assert stops == []
        assert json.loads((work / "unbound-rows.json").read_text()) == []
        assert json.loads((work / "page-bound-rows.json").read_text()) == {"222": unbound_rows[0]}
        assert json.loads((work / "verified-recurring-quantities.json").read_text()) == {
            "999": 8,
            "222": 1,
        }
        assert json.loads((work / f"{tag}-page-bound-222.json").read_text()) == {
            "product_id": "222",
            "quantity": 1,
            "displayed_price_eur": "2.00",
            "row": unbound_rows[0],
        }
        assert report["lifecycle"] == "awaiting-basket-review"
        assert {"product_id": "222", "status": "cart-covered"} in report[
            "recurring_preflight_decisions"
        ]
        manifests = list(work.glob("action-*-manifest-recurring-intent-result.json"))
        assert len(manifests) == 1  # the zero-addition completion only
    else:
        assert stops == ["planned_product_unbound_row_ambiguous:222:quantity=1:hits=2"]
        assert json.loads((work / "unbound-rows.json").read_text()) == unbound_rows
        assert not (work / "page-bound-rows.json").exists()
        if entry == "loop":
            assert (
                json.loads((work / "verified-recurring-quantities.json").read_text())
                == bound_quantities
            )
        else:
            assert not (work / "verified-recurring-quantities.json").exists()
        assert list(work.glob("action-*-manifest.json")) == []
        assert list(work.glob("*-page-bound-[0-9]*.json")) == []
        assert (
            json.loads((work / f"{tag}-page-bound-diagnostic.json").read_text())["unbound_rows"]
            == 2
        )
        assert report["lifecycle"] != "needs-attention"


def test_stage1_binds_planned_product_already_in_basket_by_page_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan_stage1.py after the baseline: the planned cheese product page shows one unit in the
    cart although the baseline bound that checkout row to no page. The row moves from the
    unbound record into the page-bound record and the bound lines, and the first preflight
    reports the product covered without planning an addition."""
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "cheese", "product_id": "222", "product_url": url("222"), "quantity": 2}
    ]
    paths.config_file.write_text(yaml.safe_dump(config))
    plan = run_plan_prepare(paths, skip_history_sync=True)
    bound_quantities = {"999": 8}
    baseline = run_baseline_apply(paths, plan["run_id"], observation(bound_quantities))
    work = tmp_path / "private-work"
    work.mkdir(mode=0o700)
    titles = {"222": "AB Cheese", "999": "AB Baseline"}
    quantities = {"999": 8, "222": 1}  # what the store basket really holds
    unbound_rows = [{"name": "Cheese", "brand": "AB", "quantity": "1", "price": "2.00"}]

    def write(name: str, data: Any) -> None:
        (work / name).write_text(json.dumps(data))

    def product(pid: str, canonical_url: str, *_: Any, **__: Any) -> dict[str, Any]:
        return {
            "product_id": pid,
            "canonical_url": canonical_url,
            "title": titles[pid],
            "availability": "available",
            "displayed_price_eur": "2.00",
            "quantity": quantities[pid],
        }

    def grossy(*args: str, out: str | None = None) -> dict[str, Any]:
        assert args[:2] == ("weekly", "recurring-preflight")
        result = run_recurring_preflight(paths, args[2], json.loads(Path(args[3]).read_text()))
        assert out is not None
        write(out, result)
        return result

    def stop(reason: str) -> None:
        pytest.fail("unexpected coordinator stop: " + reason)

    write("plan-prepare.json", plan)
    write("baseline-result.json", baseline)
    write("baseline-cart.json", observation(bound_quantities))
    write("baseline-bound.json", {"999": product("999", url("999"))})
    write("unbound-rows.json", unbound_rows)
    fakes = {"BASE": tmp_path, "write": write, "product": product, "grossy": grossy, "stop": stop}
    install_fakes(monkeypatch, work, fakes, ["plan_stage1.py"])
    runpy.run_path(str(SCRIPTS / "plan_stage1.py"))
    assert json.loads((work / "unbound-rows.json").read_text()) == []
    assert json.loads((work / "page-bound-rows.json").read_text()) == {"222": unbound_rows[0]}
    assert json.loads((work / "baseline-page-bound-222.json").read_text()) == {
        "product_id": "222",
        "quantity": 1,
        "displayed_price_eur": "2.00",
        "row": unbound_rows[0],
    }
    assert json.loads((work / "baseline-bound.json").read_text())["222"]["quantity"] == 1
    cart = json.loads((work / "recurring-preflight-cart.json").read_text())
    assert {line["product_id"]: line["quantity"] for line in cart["lines"]} == quantities
    preflight = json.loads((work / "recurring-preflight.json").read_text())
    assert preflight["manifest"]["additions"] == []
    assert {"product_id": "222", "status": "cart-covered"} in preflight["decisions"]


@pytest.mark.parametrize("outcome", ["unique", "ambiguous", "after_anchor"])
def test_recipe_loop_binds_planned_product_already_in_basket_by_page_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Recipe counterpart: the rice row no page bound at advance is the planned product.

    recipe_add.py reads the recipe product page without a quantity gate, binds the unique
    matching unbound row before the first recipe preflight and the CLI reports the ingredient
    satisfied: no recipe-intent, no action-check, no click, no top-up. With two matching rows
    the loop stops fail-closed before the preflight. ``after_anchor``: the page reads 0 before
    the preflight that anchors the CLI's recipe cart and positive on the target reread; the
    loop stops fail-closed before intent and leaves every record as read (the CLI would reject
    the new line as drift, and binding there would discard that protection).
    """
    from test_weekly_recipes import add_product, add_recipe, match, prepare, proposal

    from grossy.weekly.recipes import run_recipes_apply
    from grossy.weekly.selection import run_recipe_intent, run_recipe_preflight, run_selection_apply

    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    add_product(paths, "111", "rice")
    revision = add_recipe(paths, "one", "meat", names=["rice"])
    run_id = prepare(paths)  # empty bound cart through baseline, recurring and advance
    recipes = run_recipes_apply(paths, run_id, proposal(run_id, [match("one", revision, "111")]))
    selection = run_selection_apply(paths, run_id, "all")
    work = tmp_path / "private-work"
    work.mkdir(mode=0o700)
    rice_url = "https://store.example/en/eshop/item/p/111"
    quantities = {"111": 1}  # the store basket really holds one unbound rice pack
    unbound_rows = [{"name": "Rice", "brand": "AB", "quantity": "1", "price": "2.00"}]
    if outcome == "ambiguous":
        unbound_rows.append({"name": "Beans", "brand": "AB", "quantity": "1", "price": "2.00"})
    stale_reads = 1 if outcome == "after_anchor" else 0  # first page read shows 0 in cart
    clicks: list[str] = []
    checks: list[str] = []
    intents: list[str] = []
    stops: list[str] = []
    gates: list[tuple[str, int | None]] = []

    def write(name: str, data: Any) -> None:
        (work / name).write_text(json.dumps(data))

    def recipe_fact(pid: str, canonical_url: str, qty: int | None = None) -> dict[str, Any]:
        nonlocal stale_reads
        gates.append((pid, qty))
        if qty is not None and qty != quantities.get(pid, 0):
            pytest.fail(f"quantity gate {qty} would never settle for {pid}")
        quantity = quantities.get(pid, 0)
        if stale_reads:
            stale_reads -= 1
            quantity = 0
        return {
            "product_id": pid,
            "canonical_url": canonical_url,
            "title": "AB Rice",
            "availability": "available",
            "displayed_price_eur": "2.00",
            "package_amount": "100",
            "package_unit": "g",
            "package_count": 1,
            "sale_kind": "packaged",
            "quantity": quantity,
        }

    write("plan-prepare.json", {"run_id": run_id})
    write("recipes-apply.json", recipes)
    write("selection-apply.json", selection)
    write("recurring-readings.json", [])
    write("known-urls.json", {"111": rice_url})
    write("verified-cart-quantities.json", {})
    write("unbound-rows.json", unbound_rows)

    def grossy(*args: str, out: str | None = None) -> dict[str, Any]:
        handlers = {
            "recipe-preflight": run_recipe_preflight,
            "recipe-intent": run_recipe_intent,
            "action-check": run_action_check,
        }
        assert args[0] == "weekly"
        payload = json.loads(Path(args[3]).read_text())
        if args[1] == "action-check":
            checks.append(payload["additions"][0]["product_id"])
        if args[1] == "recipe-intent" and payload["additions"]:
            intents.append(args[3])
        result = handlers[args[1]](paths, args[2], payload)
        if out:
            write(out, result)
        return result

    def add_units(pid: str, before: int, delta: int, *_: Any) -> int:
        clicks.append(pid)
        quantities[pid] = before + delta
        return quantities[pid]

    def checkout_rows(*_: Any) -> dict[str, Any]:
        rows = [
            {"name": "Rice", "brand": "AB", "quantity": str(q), "price_parts": ["€", "2", "00"]}
            for q in quantities.values()
        ]
        rows += [
            {
                "name": u["name"],
                "brand": u["brand"],
                "quantity": "1",
                "price_parts": ["€", "2", "00"],
            }
            for u in unbound_rows[1:]
        ]
        return {
            "rows": rows,
            "empty": 0,
            "badge": [str(sum(quantities.values()) + len(unbound_rows) - 1)],
        }

    def stop(reason: str) -> None:
        stops.append(reason)
        raise SystemExit(1)

    fakes = {
        "write": write,
        "grossy": grossy,
        "recipe_fact": recipe_fact,
        "add_units": add_units,
        "stop": stop,
    }
    install_fakes(monkeypatch, work, fakes, ["recipe_add.py", "all", "5"], checkout_rows)
    if outcome == "unique":
        runpy.run_path(str(SCRIPTS / "recipe_add.py"))
    else:
        with pytest.raises(SystemExit):
            runpy.run_path(str(SCRIPTS / "recipe_add.py"))
    assert clicks == [] and checks == [] and intents == []
    assert gates and all(qty is None for _, qty in gates)  # no quantity gate on the unbound page
    assert quantities == {"111": 1}
    assert not (work / "recipe-pending.json").exists()
    report = json.loads((paths.run_dir(run_id) / "report.json").read_text())
    if outcome == "unique":
        assert stops == []
        assert json.loads((work / "unbound-rows.json").read_text()) == []
        assert json.loads((work / "page-bound-rows.json").read_text()) == {"111": unbound_rows[0]}
        assert json.loads((work / "verified-cart-quantities.json").read_text()) == {"111": 1}
        assert json.loads((work / "recipe-01-page-bound-111.json").read_text()) == {
            "product_id": "111",
            "quantity": 1,
            "displayed_price_eur": "2.00",
            "row": unbound_rows[0],
        }
        assert report["lifecycle"] == "awaiting-user-verification"
        manifests = [json.loads(p.read_text()) for p in work.glob("recipe-*-manifest.json")]
        assert [m["additions"] for m in manifests] == [[]]  # zero-addition completion only
    else:
        assert stops == [
            "planned_product_unbound_row_ambiguous:111:quantity=1:hits=2"
            if outcome == "ambiguous"
            else "recipe_product_present_after_anchor:111"
        ]
        assert json.loads((work / "unbound-rows.json").read_text()) == unbound_rows
        assert not (work / "page-bound-rows.json").exists()
        assert json.loads((work / "verified-cart-quantities.json").read_text()) == {}
        assert list(work.glob("recipe-*-manifest.json")) == []
        assert list(work.glob("recipe-*-page-bound-[0-9]*.json")) == []
        assert report["lifecycle"] != "needs-attention"
        if outcome == "after_anchor":
            assert stale_reads == 0 and [pid for pid, _ in gates] == ["111", "111"]
            assert (work / "recipe-01-cart-1-recipe-preflight-result.json").exists()
