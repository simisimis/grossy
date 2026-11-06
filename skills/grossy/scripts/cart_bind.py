"""Identity-bind checkout lines to product pages (read-only); rows no page binds are recorded
as unbound rows, never removed or adjusted.

Shared by baseline, the recurring seed and advance.
"""

import json
import re
import time
import urllib.parse

import live_common as drill
import recurring_cart
from live_common import CHECKOUT_URL, STORE, D, stop, write


def norm(t):
    return " ".join((t or "").split()).casefold()


def checkout_rows(tag="cart"):
    """Settled checkout rows incl. unavailable ('See Alternatives') lines; no private sections."""
    drill.browser(["open", CHECKOUT_URL], tag)
    prev = None
    for _ in range(40):
        time.sleep(0.75)
        c = drill.evaluate(
            'if(location.pathname!=="/checkout")throw Error("unexpected_cart_route");const remove=['
            '...document.querySelectorAll("[data-testid=remove-basket-item]")].filter(v);'
            'const rows=remove.map(btn=>{let e=btn.parentElement;while(e&&!(e.querySelectorAll("[da'
            'ta-testid=remove-basket-item]").length===1&&e.querySelectorAll("[data-testid=product-'
            'name]").length===1))e=e.parentElement;if(!e)return {ambiguous:true,reason:"missing_un'
            'ique_cart_row"};'
            'if(e.querySelectorAll("[data-testid=product-block-quantity-input]").length!==1){const '
            "t=e.innerText;return /See Alternatives|Δείτε εναλλακτικ/i.test(t)?{unavailable:true,n"
            'ame:e.querySelector("[data-testid=product-name]")?.innerText.trim(),brand:(e.querySel'
            'ector("[data-testid=product-brand]")?.innerText||"").trim()}:{ambiguous:true,reason:"'
            'missing_quantity_without_unavailable_control"};}'
            'const pr=[...e.querySelectorAll("[data-testid=product-block-price]")].filter(v);return'
            ' {name:e.querySelector("[data-testid=product-name]")?.innerText.trim(),brand:(e.query'
            'Selector("[data-testid=product-brand]")?.innerText||"").trim(),quantity:e.querySelect'
            'or("[data-testid=product-block-quantity-input]")?.value,price_parts:pr.length===1?[..'
            ".pr[0].children].map(c=>c.innerText.trim()):[]};});"
            'const empty=[...document.querySelectorAll("p,span,div,h1,h2,h3")].filter(e=>e.children'
            '.length===0&&v(e)&&!e.closest("button,a")&&'
            + drill.EMPTY_RE
            + ".test(e.textContent.trim())).length;"
            'const badge=[...document.querySelectorAll("[data-testid=item-count-in-basket-button-to'
            'ggle]")].filter(v).map(e=>e.innerText.trim());return {ready:document.readyState,rows,'
            "empty,badge};",
            tag,
        )
        sig = json.dumps(c["rows"], sort_keys=True) + str(c["badge"]) + str(c["empty"])
        if c["ready"] == "complete" and (c["rows"] or c["empty"] == 1) and sig == prev:
            if c["rows"] and c["empty"]:
                stop("empty_message_with_lines_conflict")
            return c
        prev = sig
    stop("checkout_not_settled")


def match_row(row, cand, page_row=None):
    """Row identity is the page title containing name and brand, or equality with the product's
    page-bound record ``page_row``; quantity and line price must match either way."""
    price = drill.price_of(row["price_parts"])
    q = int(row["quantity"])
    identity = (
        norm(row["name"]) in norm(cand["title"]) and norm(row["brand"]) in norm(cand["title"])
    ) or (
        page_row is not None
        and recurring_cart.unbound_key(page_row)
        == recurring_cart.unbound_key(recurring_cart.unbound_row(row))
    )
    return (
        identity
        and cand["quantity"] == q
        and price
        in (cand["displayed_price_eur"], "%.2f" % (float(cand["displayed_price_eur"]) * q))
    )


def bind_page_row(reading, prefix):
    """Page evidence outside a whole-cart read: ``reading`` is a planned product page showing a
    positive in-cart quantity for a product not among the expected lines. Move the unique
    quantity-and-price match out of ``unbound-rows.json`` into ``page-bound-rows.json`` and
    return it; an absent or ambiguous match writes ``<prefix>-page-bound-diagnostic.json`` and
    stops before intent. No browser write."""
    pid = reading["product_id"]
    unbound_file = D / recurring_cart.UNBOUND_RECORD
    page_bound_file = D / recurring_cart.PAGE_BOUND_RECORD
    unbound = json.loads(unbound_file.read_text()) if unbound_file.exists() else []
    try:
        row = recurring_cart.bind_page_evidence(reading, unbound)
    except ValueError as exc:
        write(
            prefix + "-page-bound-diagnostic.json",
            {"reason": str(exc), "unbound_rows": len(unbound)},
        )
        stop(str(exc))
    page_bound = json.loads(page_bound_file.read_text()) if page_bound_file.exists() else {}
    write(unbound_file.name, recurring_cart.without_unbound_row(unbound, row))
    write(page_bound_file.name, {**page_bound, pid: row})
    write(
        f"{prefix}-page-bound-{pid}.json",
        {
            "product_id": pid,
            "quantity": reading["quantity"],
            "displayed_price_eur": reading["displayed_price_eur"],
            "row": row,
        },
    )
    print(
        f"{prefix}: planned product {pid} already in basket x{reading['quantity']}; checkout row "
        "bound by page evidence, no write",
        flush=True,
    )
    return row


def bind_cart(known_products, tag="cart", recorded_unbound=None, page_bound=None):
    """Read whole cart; bind lines to product pages.

    known_products: pid -> {canonical_url, title} from local evidence. Only candidates whose
    stored title contains the row's name and brand, or whose ``page_bound`` record equals a
    row, are opened; the rest go through exact search. Buyable rows that neither path binds are
    returned as unbound rows (name, brand, quantity, price as read) and written to
    ``<tag>-unbound.json``. With ``recorded_unbound`` None (baseline, advance) they replace
    ``unbound-rows.json`` and ``page-bound-rows.json`` keeps only records still bound.
    Otherwise (recurring seed) a recorded unbound row that a known product's page now binds,
    by stored title or exact search, is page evidence: it moves from ``unbound-rows.json``
    into ``page-bound-rows.json`` when it is the unique quantity-and-price match; the
    remaining rows must equal the recorded list, which is otherwise untouched. The
    observation holds bound lines only.
    Returns (observation, bound dict pid->reading, unavailable rows, rows_bound_via_search,
    unbound rows)."""
    known_urls = {p: k["canonical_url"] for p, k in known_products.items()}
    page_bound = page_bound or {}
    start = drill.now()
    c1 = checkout_rows(tag)
    if any(r.get("ambiguous") for r in c1["rows"]):
        stop("ambiguous_checkout_line")
    unavailable = [r for r in c1["rows"] if r.get("unavailable")]
    rows = [r for r in c1["rows"] if not r.get("unavailable")]
    write(tag + "-unavailable-lines.json", unavailable)
    bound, unknown, bound_rows, known = {}, [], {}, {}
    if rows:
        # Known candidates first: hard reload each product page and bind by
        # title/brand/quantity/price (or the page-bound record and quantity/price).
        for pid, k in known_products.items():
            t = norm(k.get("title"))
            if (t and any(norm(r["name"]) in t and norm(r["brand"]) in t for r in rows)) or any(
                pid in recurring_cart.page_bound_ids(r, page_bound) for r in rows
            ):
                known[pid] = drill.product(pid, k["canonical_url"], None, force_reload=True)
        for row in rows:
            ids = [
                p
                for p, cnd in known.items()
                if cnd and cnd["quantity"] and match_row(row, cnd, page_bound.get(p))
            ]
            if len(ids) == 1 and ids[0] not in bound:
                bound[ids[0]] = known[ids[0]]
                bound_rows[ids[0]] = row
            elif not ids:
                unknown.append(row)
            else:
                stop("ambiguous_known_binding")
        for row in list(unknown):
            stale = [
                p
                for p, cnd in known.items()
                if cnd
                and not cnd["quantity"]
                and norm(row["name"]) in norm(cnd["title"])
                and norm(row["brand"]) in norm(cnd["title"])
            ]
            if len(stale) == 1:
                r = drill.product(stale[0], known_urls[stale[0]], None, force_reload=True)
                known[stale[0]] = r
                if r["quantity"] and match_row(row, r) and stale[0] not in bound:
                    bound[stale[0]] = r
                    bound_rows[stale[0]] = row
                    unknown.remove(row)
        missing = [p for p, cnd in known.items() if cnd and cnd["quantity"] and p not in bound]
        if missing:
            stop("known_in_cart_not_in_checkout:" + ",".join(missing))
    searched, unbound = [], []
    for row in unknown:
        e = urllib.parse.quote(row["name"])
        drill.browser(
            ["open", f"{STORE}/eshop/search?q={e}%3Arelevance&text={e}&sort=relevance"],
            tag + "s",
        )
        sig = None
        for _ in range(20):
            time.sleep(0.7)
            s = drill.evaluate(
                'return location.pathname==="/eshop/search"&&document.readyState==="complete"?[...d'
                'ocument.querySelectorAll("[data-testid=product-block] [data-testid=product-name]"'
                ')].filter(v).slice(0,8).map(e=>e.innerText.trim()).join("|"):null;',
                tag + "s",
            )
            if s and s == sig:
                break
            sig = s
        cards = drill.evaluate(
            'return [...document.querySelectorAll("[data-testid=product-block]")].filter(v).map(c=>'
            "{const a=[...c.querySelectorAll(\"a[href*='/p/']\")].map(x=>x.href.split(/[?#]/)[0])."
            'find(h=>/\\/p\\/\\d+$/.test(h));return {url:a||null,name:c.querySelector("[data-testi'
            'd=product-name]")?.innerText.trim(),brand:(c.querySelector("[data-testid=product-bran'
            'd]")?.innerText||"").trim()}});',
            tag + "s",
        )
        cands = [
            cd
            for cd in cards
            if cd["url"]
            and norm(cd["name"]) == norm(row["name"])
            and norm(cd["brand"]) == norm(row["brand"])
        ]
        hits = []
        for cd in {cd["url"]: cd for cd in cands}.values():
            pid = re.search(r"/p/(\d+)$", cd["url"]).group(1)
            if pid in bound:
                continue
            r = drill.product(pid, cd["url"], None, force_reload=True)
            if r["quantity"] and match_row(row, r):
                hits.append(r)
        if len(hits) != 1:
            unbound.append(recurring_cart.unbound_row(row))
            continue
        bound[hits[0]["product_id"]] = hits[0]
        bound_rows[hits[0]["product_id"]] = row
        searched.append(hits[0])
    write(tag + "-unbound.json", unbound)
    c2 = checkout_rows(tag + "2")
    c2rows = [r for r in c2["rows"] if not r.get("unavailable")]
    if json.dumps(c2rows, sort_keys=True) != json.dumps(rows, sort_keys=True):
        stop("checkout_changed_during_read")
    total = sum(int(r["quantity"]) for r in c2rows)
    if rows and c2["badge"] not in ([str(total)], [str(total + len(unavailable))]):
        stop("header_badge_inconsistent")
    if recorded_unbound is None:
        write(recurring_cart.UNBOUND_RECORD, unbound)
        write(
            recurring_cart.PAGE_BOUND_RECORD,
            {
                p: r
                for p, r in page_bound.items()
                if p in bound_rows
                and recurring_cart.unbound_key(recurring_cart.unbound_row(bound_rows[p]))
                == recurring_cart.unbound_key(r)
            },
        )
    else:
        # Recorded unbound rows now bound to known (planned) products; a row bound to any other
        # product is still a changed unbound list below.
        recorded_keys = set(map(recurring_cart.unbound_key, recorded_unbound))
        transitions = {}
        for p, row in bound_rows.items():
            if (
                p not in known_products
                or p in page_bound
                or recurring_cart.unbound_key(recurring_cart.unbound_row(row)) not in recorded_keys
            ):
                continue
            try:
                transitions[p] = recurring_cart.bind_page_evidence(bound[p], recorded_unbound)
            except ValueError as exc:
                write(
                    tag + "-page-bound-diagnostic.json",
                    {"reason": str(exc), "unbound_rows": len(recorded_unbound)},
                )
                stop(str(exc))
        remaining_unbound = list(recorded_unbound)
        for row in transitions.values():
            remaining_unbound = recurring_cart.without_unbound_row(remaining_unbound, row)
        recorded = sorted(map(recurring_cart.unbound_key, remaining_unbound))
        current = sorted(map(recurring_cart.unbound_key, unbound))
        if recorded != current:
            stop(f"checkout_unbound_rows_changed:recorded={len(recorded)}:current={len(current)}")
        for p, row in transitions.items():
            write(
                f"{tag}-page-bound-{p}.json",
                {
                    "product_id": p,
                    "quantity": bound[p]["quantity"],
                    "displayed_price_eur": bound[p]["displayed_price_eur"],
                    "row": row,
                },
            )
            print(
                f"{tag}: planned product {p} already in basket x{bound[p]['quantity']}; checkout "
                "row bound by page evidence, no write",
                flush=True,
            )
        if transitions:
            write(recurring_cart.UNBOUND_RECORD, remaining_unbound)
            write(recurring_cart.PAGE_BOUND_RECORD, {**page_bound, **transitions})
    obs = {
        "schema_name": "grossy.cart.observation.v1",
        "schema_version": 1,
        "generated_at": start,
        "lines": [
            {
                "product_id": p,
                "canonical_url": r["canonical_url"],
                "quantity": r["quantity"],
                "displayed_price_eur": r["displayed_price_eur"],
            }
            for p, r in bound.items()
        ],
    }
    return obs, bound, unavailable, searched, unbound
