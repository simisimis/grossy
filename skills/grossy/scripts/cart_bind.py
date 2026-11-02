"""Identity-bind every checkout line to a product page (read-only).

Shared by baseline and advance.
"""

import json
import re
import time
import urllib.parse

import live_common as drill
from live_common import stop, write


def norm(t):
    return " ".join((t or "").split()).casefold()


def checkout_rows(tag="cart"):
    """Settled checkout rows incl. unavailable ('See Alternatives') lines; no private sections."""
    drill.browser(["open", "https://www.ab.gr/checkout"], tag)
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


def match_row(row, cand):
    price = drill.price_of(row["price_parts"])
    q = int(row["quantity"])
    return (
        norm(row["name"]) in norm(cand["title"])
        and norm(row["brand"]) in norm(cand["title"])
        and cand["quantity"] == q
        and price
        in (cand["displayed_price_eur"], "%.2f" % (float(cand["displayed_price_eur"]) * q))
    )


def bind_cart(known_products, tag="cart"):
    """Read whole cart; bind lines to product pages.

    known_products: pid -> {canonical_url, title} from local evidence. Only candidates whose
    stored title contains the row's name and brand are opened; the rest go through exact search.
    Returns (observation, bound dict pid->reading, unavailable rows, rows_bound_via_search)."""
    known_urls = {p: k["canonical_url"] for p, k in known_products.items()}
    start = drill.now()
    c1 = checkout_rows(tag)
    if any(r.get("ambiguous") for r in c1["rows"]):
        stop("ambiguous_checkout_line")
    unavailable = [r for r in c1["rows"] if r.get("unavailable")]
    rows = [r for r in c1["rows"] if not r.get("unavailable")]
    write(tag + "-unavailable-lines.json", unavailable)
    bound, unknown = {}, []
    if rows:
        # Known candidates first: hard reload each product page and bind by
        # title/brand/quantity/price.
        known = {}
        for pid, k in known_products.items():
            t = norm(k.get("title"))
            if t and any(norm(r["name"]) in t and norm(r["brand"]) in t for r in rows):
                known[pid] = drill.product(pid, k["canonical_url"], None, force_reload=True)
        for row in rows:
            ids = [p for p, cnd in known.items() if cnd and cnd["quantity"] and match_row(row, cnd)]
            if len(ids) == 1 and ids[0] not in bound:
                bound[ids[0]] = known[ids[0]]
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
                    unknown.remove(row)
        missing = [p for p, cnd in known.items() if cnd and cnd["quantity"] and p not in bound]
        if missing:
            stop("known_in_cart_not_in_checkout:" + ",".join(missing))
    searched = []
    for row in unknown:
        e = urllib.parse.quote(row["name"])
        drill.browser(
            ["open", f"https://www.ab.gr/eshop/search?q={e}%3Arelevance&text={e}&sort=relevance"],
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
            write(tag + "-unbound.json", {"row": row, "candidates": len(cands), "hits": len(hits)})
            stop("cart_line_not_bound:" + row["name"])
        bound[hits[0]["product_id"]] = hits[0]
        searched.append(hits[0])
    c2 = checkout_rows(tag + "2")
    c2rows = [r for r in c2["rows"] if not r.get("unavailable")]
    if json.dumps(c2rows, sort_keys=True) != json.dumps(rows, sort_keys=True):
        stop("checkout_changed_during_read")
    total = sum(int(r["quantity"]) for r in c2rows)
    if rows and c2["badge"] not in ([str(total)], [str(total + len(unavailable))]):
        stop("header_badge_inconsistent")
    if len(bound) != len(rows):
        stop("line_count_binding_mismatch")
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
    return obs, bound, unavailable, searched
