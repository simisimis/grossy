"""Coordinator live helpers: exact task tab, boolean readiness predicates, read-only page reads,
one bounded click.

Work directory: $GROSSY_LIVE_DIR, else the path in <state>/grossy/live-current.txt (created by
live_start.py). Browser session name: <workdir>/session.txt (written by live_start.py). Nothing
here writes to the cart unless click_once/add_units is called by a stage script after a persisted
intent and an authorized action-check.
"""

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time

os.umask(0o077)
BASE = (
    pathlib.Path(os.environ.get("XDG_STATE_HOME", str(pathlib.Path.home() / ".local/state")))
    / "grossy"
)
_ptr = BASE / "live-current.txt"
if os.environ.get("GROSSY_LIVE_DIR"):
    D = pathlib.Path(os.environ["GROSSY_LIVE_DIR"])
elif _ptr.exists():
    D = pathlib.Path(_ptr.read_text().strip())
else:
    print("STOP: no live work directory; run live.sh start", flush=True)
    sys.exit(1)
SESSION = (D / "session.txt").read_text().strip() if (D / "session.txt").exists() else "grossy-live"
V = (
    "const v=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height"
    '>0&&s.visibility!=="hidden"&&s.display!=="none"};'
)
GUARD = (
    'if(location.origin!=="https://www.ab.gr")throw Error("unexpected_origin");'
    'if([...document.querySelectorAll("input[type=password]")].some(v)||[...document.querySelectorA'
    'll("iframe[src*=captcha],[class*=captcha],[id*=captcha]")]'
    ".some(e=>v(e)&&e.getBoundingClientRect().width>250&&e.getBoundingClientRect().height>150))thro"
    'w Error("authentication_or_challenge");'
)
EMPTY_RE = r"/^(Your (shopping )?(cart|basket) is empty[.!]?|Το καλάθι σας είναι άδειο[.!]?)$/i"


def stop(reason):
    print("STOP:", reason, flush=True)
    sys.exit(1)


def write(name, data):
    (D / name).write_text(json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n")


PROFILE = (
    pathlib.Path(os.environ.get("XDG_DATA_HOME", str(pathlib.Path.home() / ".local/share")))
    / "grossy"
    / "agent-browser"
    / "ab-profile"
)
BRAVE = pathlib.Path.home() / ".nix-profile" / "bin" / "brave"


def _tabs():
    p = subprocess.run(
        ["agent-browser", "--session", SESSION, "tab", "list", "--json"],
        capture_output=True,
        text=True,
    )
    try:
        d = json.loads(p.stdout)
        d = d.get("data", d)
        return d.get("tabs", d) if isinstance(d, dict) else d
    except ValueError:
        return None


def recover_closed_window():
    """Owner closed the window: only blank tabs remain. Relaunch headed Brave with the profile
    and rebind. Any other tab state is ambiguous and stops (never adopt a tab we did not open)."""
    tabs = _tabs()
    if tabs is None or any(str(t.get("url")) != "about:blank" for t in tabs):
        stop("exact_task_tab_unavailable_and_tabs_ambiguous")
    subprocess.run(["agent-browser", "--session", SESSION, "close"], capture_output=True, text=True)
    p = subprocess.run(
        [
            "agent-browser",
            "--session",
            SESSION,
            "--headed",
            "--executable-path",
            str(BRAVE),
            "--profile",
            str(PROFILE),
            "--pin-tab",
            "open",
            "https://www.ab.gr/checkout",
        ],
        capture_output=True,
        text=True,
    )
    tabs = _tabs() if p.returncode == 0 else None
    if not tabs or len(tabs) != 1:
        stop("relaunch_failed")
    (D / "task-tab-target.json").write_text(json.dumps({"target_id": tabs[0]["targetId"]}))
    print(
        "browser window had been closed: relaunched with profile and rebound task tab", flush=True
    )


def browser(args, tag="cmd"):
    target = json.loads((D / "task-tab-target.json").read_text())["target_id"]
    if subprocess.run(
        ["agent-browser", "--session", SESSION, "--pin-tab", "tab", target],
        capture_output=True,
        text=True,
    ).returncode:
        recover_closed_window()
        target = json.loads((D / "task-tab-target.json").read_text())["target_id"]
        if subprocess.run(
            ["agent-browser", "--session", SESSION, "--pin-tab", "tab", target],
            capture_output=True,
            text=True,
        ).returncode:
            stop("exact_task_tab_unavailable")
    p = subprocess.run(
        ["agent-browser", "--session", SESSION, "--pin-tab", *args], capture_output=True, text=True
    )
    if p.returncode:
        detail = p.stdout + p.stderr
        reason = next(
            (
                s
                for s in ["unexpected_origin", "authentication_or_challenge", "tab_gone", "imeout"]
                if s in detail
            ),
            "browser_command_error",
        )
        line = next((entry for entry in (p.stderr + p.stdout).splitlines() if entry.strip()), "")
        line = re.sub(r"https?://\S+|/home/\S+|/tmp/\S+", "[redacted]", line)[:240]
        write(
            f"{tag}-browser-error.json",
            {"command": args[0], "reason": reason, "tool_diagnostic": line},
        )
        stop(args[0] + ":" + reason)
    return p.stdout


def evaluate(body, tag="eval"):
    out = browser(["eval", "(() => {" + V + GUARD + body + "})()"], tag)
    try:
        return json.loads(out)
    except ValueError:
        stop("invalid_browser_result")


def wait(body, seconds=45, tag="wait"):
    deadline = time.monotonic() + seconds
    while True:
        r = evaluate(body, tag)
        if r is True:
            return
        if r is not False:
            stop("readiness_predicate_not_boolean")
        if time.monotonic() >= deadline:
            stop("bounded_readiness_timeout")
        time.sleep(0.25)


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def price_of(parts):
    if (
        len(parts) != 3
        or parts[0] not in ("€", "~€")
        or not re.fullmatch(r"\d+", parts[1])
        or not re.fullmatch(r"\d{2}", parts[2])
    ):
        return None
    return parts[1] + "." + parts[2]


def product(pid, url, required_quantity=None, force_reload=False):
    """Read one product page. Returns availability/price/quantity/title; no writes."""
    browser(["open", url])
    if force_reload or (required_quantity or 0) > 0:
        browser(["reload"])
    gate = (
        ""
        if required_quantity is None
        else (
            "&&readyEmpty"
            if required_quantity == 0
            else "&&readyInCart&&Number(q[0].value)===" + str(required_quantity)
        )
    )
    wait(
        'if(!location.pathname.endsWith("/p/'
        + pid
        + '"))throw Error("unexpected_product");const main=s=>[...document.querySelectorAll(s)].fil'
        'ter(e=>v(e)&&!e.closest("[data-testid=product-block],[data-testid=product-carousel-cont'
        'ainer]"));'
        'const q=main("[data-testid=product-block-quantity-input]"),add=main("[data-testid=product-'
        'block-add]"),un=main("[data-testid=product-block-unavailable-text]");'
        'const readyEmpty=q.length===0&&add.length===1&&!add[0].disabled&&add[0].getAttribute("aria'
        '-disabled")!=="true";'
        "const readyInCart=q.length===1&&add.length===0&&/^[1-9][0-9]*$/.test(q[0].value)&&(q[0].ge"
        'tAttribute("data-quantity")===null||q[0].getAttribute("data-quantity")===q[0].value);'
        'return document.readyState==="complete"&&main("[data-testid=product-common-header-title]")'
        '.length===1&&main("[data-testid=product-properties]").length===1&&(un.length===1||((ready'
        "Empty||readyInCart)" + gate + "));",
        tag="product-" + pid,
    )
    r = evaluate(
        'if(!location.pathname.endsWith("/p/'
        + pid
        + '"))throw Error("unexpected_product");const main=s=>[...document.querySelectorAll(s)].fil'
        'ter(e=>v(e)&&!e.closest("[data-testid=product-block],[data-testid=product-carousel-cont'
        'ainer]"));'
        'const panels=main("[data-testid=product-properties]");const prices=panels.flatMap(e=>[...e'
        '.querySelectorAll("[data-testid=product-block-price]")].filter(v));const titles=main("[da'
        'ta-testid=product-common-header-title]");'
        'const q=main("[data-testid=product-block-quantity-input]"),add=main("[data-testid=product-'
        'block-add]");'
        "return {url:location.origin+location.pathname,title_count:titles.length,title:titles.lengt"
        "h===1?titles[0].innerText:null,panel_count:panels.length,price_count:prices.length,parts:"
        "prices.length===1?[...prices[0].children].map(e=>e.innerText.trim()):[],"
        "quantity_count:q.length,quantity:q.length===1?q[0].value:null,data_quantity:q.length===1?q"
        '[0].getAttribute("data-quantity"):null,add_count:add.length,add_enabled:add.length===1&&!'
        'add[0].disabled&&add[0].getAttribute("aria-disabled")!=="true",unavailable:main("[data-te'
        'stid=product-block-unavailable-text]").length};',
        tag="product-" + pid,
    )
    if r["title_count"] != 1 or r["panel_count"] != 1 or r["url"] != url:
        stop("product_identity_ambiguous:" + pid)
    if r["unavailable"] == 1 and r["price_count"] == 0 and r["quantity_count"] == 0:
        return {
            "product_id": pid,
            "canonical_url": url,
            "availability": "unavailable",
            "displayed_price_eur": None,
            "quantity": 0,
            "title": r["title"],
        }
    if r["unavailable"] != 0 or r["price_count"] != 1:
        stop("purchase_panel_ambiguous:" + pid)
    price = price_of(r["parts"])
    if price is None:
        stop("ambiguous_price:" + pid)
    if r["quantity_count"] == 0 and r["add_count"] == 1 and r["add_enabled"]:
        quantity = 0
    elif (
        r["quantity_count"] == 1
        and r["add_count"] == 0
        and re.fullmatch(r"[1-9]\d*", r["quantity"] or "")
    ):
        quantity = int(r["quantity"])
        if r["data_quantity"] is not None and r["data_quantity"] != str(quantity):
            stop("inconsistent_product_quantity:" + pid)
    else:
        stop("ambiguous_product_cart_state:" + pid)
    return {
        "product_id": pid,
        "canonical_url": url,
        "availability": "available",
        "displayed_price_eur": price,
        "quantity": quantity,
        "title": r["title"],
    }


def grossy(*args, out=None, expect_exit=(0,)):
    p = subprocess.run(["grossy", *args], capture_output=True, text=True)
    if p.returncode not in expect_exit:
        if out:
            (D / (out + ".stderr")).write_text(re.sub(r"/home/\S+", "[path]", p.stderr)[-2000:])
        stop(f"CLI {args[0]} {args[1] if len(args) > 1 else ''} exit {p.returncode}")
    try:
        r = json.loads(p.stdout)
    except ValueError:
        stop("CLI_malformed_output")
    if out:
        write(out, r)
    return r


def recipe_fact(pid, url, qty=None):
    """Read-only recipe product fact incl. rendered package size.

    Weighed '~€' price is approximate per increment."""
    browser(["open", url], "rfact")
    if qty:
        browser(["reload"], "rfact")
    wait(
        'if(!location.pathname.endsWith("/p/'
        + pid
        + '"))throw Error("unexpected_product");const main=s=>[...document.querySelectorAll(s)].fil'
        'ter(e=>v(e)&&!e.closest("[data-testid=product-block],[data-testid=product-carousel-cont'
        'ainer]"));'
        'const q=main("[data-testid=product-block-quantity-input]"),add=main("[data-testid=product-'
        'block-add]"),un=main("[data-testid=product-block-unavailable-text]");'
        'return document.readyState==="complete"&&main("[data-testid=product-common-header-title]")'
        '.length===1&&main("[data-testid=product-properties]").length===1&&(un.length===1||(add.le'
        "ngth===1&&q.length===0)||(q.length===1&&add.length===0&&/^[1-9][0-9]*$/.test(q[0].value))"
        ")"
        + (
            ""
            if qty is None
            else (
                "&&q.length===0" if qty == 0 else "&&q.length===1&&Number(q[0].value)===" + str(qty)
            )
        )
        + ";",
        tag="rfact-" + pid,
    )
    r = evaluate(
        'const main=s=>[...document.querySelectorAll(s)].filter(e=>v(e)&&!e.closest("[data-testid=p'
        'roduct-block],[data-testid=product-carousel-container]"));const p=main("[data-testid=prod'
        'uct-properties]")[0];'
        'const txt=t=>[...p.querySelectorAll("[data-testid=\'"+t+"\']")].map(e=>e.textContent.trim('
        ').replace(/\\s+/g," "));const pr=[...p.querySelectorAll("[data-testid=product-block-price'
        ']")].filter(v);'
        'const q=main("[data-testid=product-block-quantity-input]");return {url:location.origin+loc'
        'ation.pathname,title:main("[data-testid=product-common-header-title]")[0]?.innerText,part'
        "s:pr.length===1?[...pr[0].children].map(e=>e.innerText.trim()):null,price_count:pr.length"
        ',supp:txt("product-block-supplementary-price"),per:txt("product-block-price-per-unit"),un'
        'available:main("[data-testid=product-block-unavailable-text]").length,qty:q.length===1?Nu'
        "mber(q[0].value):0};",
        "rfact-" + pid,
    )
    if r["url"] != url:
        stop("product_identity:" + pid)
    out = {"product_id": pid, "canonical_url": url, "title": r["title"], "quantity": r["qty"]}
    if r["unavailable"]:
        return {
            **out,
            "availability": "unavailable",
            "displayed_price_eur": None,
            "package_amount": None,
            "package_unit": None,
            "package_count": None,
            "sale_kind": None,
        }
    if r["price_count"] != 1 or not r["parts"] or len(r["parts"]) != 3:
        stop("ambiguous_price:" + pid)
    weighed = r["parts"][0] == "~€"
    if (
        r["parts"][0] not in ("€", "~€")
        or not re.fullmatch(r"\d+", r["parts"][1])
        or not re.fullmatch(r"\d{2}", r["parts"][2])
    ):
        stop("ambiguous_price:" + pid)
    price = r["parts"][1] + "." + r["parts"][2]
    amount = unit = None
    m = re.fullmatch(
        r"~?\s*(\d+(?:[.,]\d+)?)\s*(gr|g|kg|ml|lt|l)",
        (r["supp"][0] if r["supp"] else "").strip(),
        re.I,
    )
    if m:
        amount = m.group(1).replace(",", ".")
        unit = {"gr": "g", "g": "g", "kg": "kg", "ml": "ml", "lt": "l", "l": "l"}[
            m.group(2).lower()
        ]
    elif r["per"] and r["per"][0].endswith("/pc"):
        pc = re.search(r"(\d+)\s*(?:Pieces|pcs|τεμ)", r["title"] or "", re.I)
        amount, unit = (pc.group(1), "pieces") if pc else ("1", "piece")
    return {
        **out,
        "availability": "available",
        "displayed_price_eur": price,
        "package_amount": amount,
        "package_unit": unit,
        "package_count": 1 if amount else None,
        "sale_kind": ("weighed" if weighed else "packaged") if amount else None,
        "supp_text": r["supp"][:1],
        "per_text": r["per"][:1],
    }


BACKOFF = (1, 2, 5)  # seconds before click, per attempt; each attempt is a separate intent
MAIN = ':not([data-testid="product-block"] *):not([data-testid="product-carousel-container"] *)'
ADD_SEL = '[data-testid="product-block-add"]' + MAIN


def settle_page(tag="settle"):
    """Wait for network idle (bounded).

    A busy page is not an error, the timed delay still applies."""
    target = json.loads((D / "task-tab-target.json").read_text())["target_id"]
    subprocess.run(
        ["agent-browser", "--session", SESSION, "--pin-tab", "tab", target],
        capture_output=True,
        text=True,
    )
    p = subprocess.run(
        ["agent-browser", "--session", SESSION, "--pin-tab", "wait", "--load", "networkidle"],
        capture_output=True,
        text=True,
        timeout=40,
    )
    return p.returncode == 0


def click_once(pid, expected_after, delay, tag, sel=ADD_SEL):
    """Exactly one click on the exact product page after settle+delay; poll ≤15 s for the
    stepper. Never re-clicks."""
    evaluate(
        'if(!location.pathname.endsWith("/p/'
        + pid
        + '"))throw Error("unexpected_product");return true;',
        tag,
    )
    settle_page(tag)
    time.sleep(delay)
    if evaluate("return [...document.querySelectorAll('" + sel + "')].filter(v).length;", tag) != 1:
        stop("control_ambiguous:" + pid)
    evaluate(
        "document.querySelector('" + sel + "').scrollIntoView({block:'center'});return true;", tag
    )
    time.sleep(1.0)
    browser(["click", sel], tag + "-click")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(0.5)
        val = evaluate(
            'const q=[...document.querySelectorAll("[data-testid=product-block-quantity-input]")].f'
            'ilter(e=>v(e)&&!e.closest("[data-testid=product-block],[data-testid=product-carousel-'
            'container]"));return q.length===1?q[0].value:null;',
            tag,
        )
        if val == str(expected_after):
            # The site can show a press and then drop it; require the value to hold.
            time.sleep(2.5)
            held = evaluate(
                'const q=[...document.querySelectorAll("[data-testid=product-block-quantity-input]"'
                ')].filter(e=>v(e)&&!e.closest("[data-testid=product-block],[data-testid=product-c'
                'arousel-container]"));return q.length===1?q[0].value:null;',
                tag,
            )
            return held == str(expected_after)
    return False


def probe_quantity(pid, url):
    """Read-only: hard reload and report the settled in-cart quantity (0 if Add shown)."""
    browser(["open", url], "probe")
    browser(["reload"], "probe")
    settle_page("probe")
    wait(
        'const main=s=>[...document.querySelectorAll(s)].filter(e=>v(e)&&!e.closest("[data-testid=p'
        'roduct-block],[data-testid=product-carousel-container]"));return document.readyState==="c'
        'omplete"&&(main("[data-testid=product-block-add]").length+main("[data-testid=product-bloc'
        'k-quantity-input]").length)===1;',
        tag="probe",
    )
    return evaluate(
        'const q=[...document.querySelectorAll("[data-testid=product-block-quantity-input]")].filte'
        'r(e=>v(e)&&!e.closest("[data-testid=product-block],[data-testid=product-carousel-containe'
        'r]"));return q.length===1?Number(q[0].value):0;',
        "probe",
    )


INC_SEL = '[data-testid="product-block-quantity-increase"]' + MAIN


def add_units(pid, before, quantity, delay, tag):
    """One Add (if absent) then '+1' presses until before+quantity; each press waits for the
    stepper. Returns the last confirmed quantity (never presses again after a press fails to
    register)."""
    have = before
    for step in range(quantity):
        sel = ADD_SEL if have == 0 else INC_SEL
        if not click_once(
            pid, have + 1, delay if step == 0 else max(delay, 2), f"{tag}-u{step + 1}", sel
        ):
            return have
        have += 1
    return have
