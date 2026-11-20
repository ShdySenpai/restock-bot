#!/usr/bin/env python3
"""
Restock Bot -> Discord alerts
=============================
Watches any product page (cards, games, shoes, whatever) listed in
watchlist.yaml and posts to Discord when something comes BACK in stock,
with the product link, the store price, MSRP, and what it's going for on eBay.

Run once (this is what GitHub Actions does every 15 minutes):
    python3 monitor.py

Run on your Mac and keep checking every 60 seconds:
    python3 monitor.py --loop 60

Send a test message to Discord:
    python3 monitor.py --test

Items come from watchlist.yaml AND (optionally) a Google Sheet filled by a
Google Form, published as CSV. Its link goes in SHEET_CSV_URL.

Settings (DISCORD_WEBHOOK_URL, SHEET_CSV_URL, BESTBUY_API_KEY, eBay keys) are read from GitHub
secrets, or from a local .env file that never gets uploaded.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
WATCHLIST_FILE = ROOT / "watchlist.yaml"
STATE_FILE = ROOT / "state.json"

# Words that mean "you can buy this" / "you can't buy this". Used when a page
# doesn't publish its stock status in a machine-readable way.
DEFAULT_IN_STOCK = [
    "add to cart", "add to bag", "add to basket", "ship it", "pick it up",
    "buy now", "pre-order now", "preorder now",
]
# Strong phrases: trusted on buttons (the main buy button says one of these when you can't buy).
DEFAULT_OUT_OF_STOCK = [
    "out of stock", "sold out", "coming soon", "notify me", "email me when available",
]
# Weak phrases: only used on the plain page text, never on buttons, because stores put
# them on delivery/pickup option buttons (e.g. Target's "Delivery: not available") even
# when the item can be shipped.
WEAK_OUT_OF_STOCK = ["currently unavailable", "not available", "unavailable"]
# Signs that the store showed a bot check instead of the product page.
BLOCK_MARKERS = [
    "robot or human", "are you a human", "access denied", "verify you are human",
    "pardon our interruption", "request unsuccessful", "captcha",
    "unusual traffic", "please enable js and disable any ad blocker",
]

BLOCKED_WARN_AFTER = 12   # consecutive failed checks before warning Discord (12 x 15 min = 3 hours)
GREEN, RED, YELLOW, BLUE = 0x2ECC71, 0xE74C3C, 0xF1C40F, 0x3498DB

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Settings, files, small helpers
# ---------------------------------------------------------------------------

def load_dotenv():
    """Read KEY=value lines from a local .env file (for running on your Mac)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def store_name(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    known = {
        "target.com": "Target", "walmart.com": "Walmart", "bestbuy.com": "Best Buy",
        "pokemoncenter.com": "Pokémon Center", "gamestop.com": "GameStop",
        "amazon.com": "Amazon", "nike.com": "Nike", "footlocker.com": "Foot Locker",
        "costco.com": "Costco", "samsclub.com": "Sam's Club",
    }
    return known.get(host, host)


def money(value):
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "—"


def to_float(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json was unreadable — starting fresh.")
    return {"items": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


# Google Form answers land in a sheet with question titles as column headers.
# Each field is found by a keyword in its column title, so the exact wording doesn't matter.
SHEET_COLUMNS = {
    "name": ("product name", "name of", "product", "item", "name"),
    "url": ("link", "url"),
    "msrp": ("msrp", "retail"),
    "max_price": ("max", "budget"),
    "added_by": ("your name", "added by", "who"),
}


def read_sheet():
    """Items from the Google Sheet behind the Google Form. Returns (items, error)."""
    url = os.environ.get("SHEET_CSV_URL", "").strip()
    if not url:
        return [], None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        r.encoding = "utf-8"
        rows = list(csv.reader(io.StringIO(r.text)))
    except Exception as e:  # noqa: BLE001
        return None, f"couldn't download the Google Sheet: {e}"
    if not rows:
        return [], None

    headers = [h.strip().lower() for h in rows[0]]
    columns = {}
    for field, keywords in SHEET_COLUMNS.items():
        for keyword in keywords:  # first keyword wins, and a column is only used once
            match = next((i for i, h in enumerate(headers)
                          if keyword in h and i not in columns.values()), None)
            if match is not None:
                columns[field] = match
                break
    if "name" not in columns or "url" not in columns:
        return None, "the Google Sheet needs a product name column and a link column"

    items = []
    for row in rows[1:]:
        get = lambda f: row[columns[f]].strip() if f in columns and columns[f] < len(row) else ""  # noqa: E731
        if not get("name") and not get("url"):
            continue  # blank row
        item = {"name": get("name"), "url": get("url"), "source": "Google Form"}
        for field in ("msrp", "max_price", "added_by"):
            if get(field):
                item[field] = get(field)
        items.append(item)
    return items, None


def load_watchlist(state):
    """Valid items from watchlist.yaml + the Google Form sheet.

    Returns (items, complete). complete is False if a source couldn't be read, so
    items missing from this run aren't forgotten. Posts one Discord message per new problem.
    """
    errors, complete = [], True
    try:
        data = yaml.safe_load(WATCHLIST_FILE.read_text()) or []
        if not isinstance(data, list):
            raise ValueError("the file should be a list of items, each starting with '- name:'")
    except Exception as e:  # noqa: BLE001 — show any problem to the humans
        data, complete = [], False
        errors.append(f"watchlist.yaml: {e}")

    sheet_items, sheet_error = read_sheet()
    if not os.environ.get("SHEET_CSV_URL", "").strip():
        log("Google Sheet: not set up (no SHEET_CSV_URL secret) — only watchlist.yaml is used")
    elif sheet_items is not None:
        log(f"Google Sheet: read OK, {len(sheet_items)} item(s) from the form")
    log(f"watchlist.yaml: {len(data)} item(s)")
    if sheet_error:
        errors.append(sheet_error)
        complete = False
    data = data + (sheet_items or [])

    items, problems, urls = [], [], set()
    for i, entry in enumerate(data, 1):
        where = "Google Form" if isinstance(entry, dict) and entry.get("source") else f"watchlist.yaml item #{i}"
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
            problems.append(f"{where} needs both a name and a link")
            continue
        url = str(entry["url"]).strip()
        if not url.startswith("http"):
            problems.append(f"\"{entry['name']}\" ({where}): the link should start with https://")
            continue
        if url in urls:
            continue  # same link added twice — watch it once
        urls.add(url)
        items.append(entry)

    message = "\n".join(errors + problems) or None
    digest = hashlib.sha1(message.encode()).hexdigest()[:10] if message else None
    if message and state.get("watchlist_error") != digest:
        send_discord(embed={
            "title": "⚠️ Problem with the watchlist",
            "description": f"```{message[:1500]}```\nEverything else keeps working. "
                           "Fix the item (in the Google Sheet or watchlist.yaml) and this clears up.",
            "color": YELLOW,
        })
    state["watchlist_error"] = digest
    if message:
        log(f"watchlist problem: {message}")
    return items, complete


def item_key(item):
    return str(item["url"]).strip()


# ---------------------------------------------------------------------------
# Reading a product page
# ---------------------------------------------------------------------------

# Runs inside the page: collects stock info the store publishes for search
# engines (schema.org JSON-LD), price meta tags, and the visible buttons in page order.
PAGE_SCRIPT = r"""
() => {
  const out = {availability: [], prices: [], currency: null, buttons: []};
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    const offers = node.offers;
    if (offers) {
      (Array.isArray(offers) ? offers : [offers]).forEach(o => {
        if (!o || typeof o !== 'object') return;
        if (o.availability) out.availability.push(String(o.availability));
        const p = o.price ?? o.lowPrice;
        if (p !== undefined && p !== null && p !== '') out.prices.push(String(p));
        if (o.priceCurrency) out.currency = o.priceCurrency;
        if (o.offers) walk(o.offers);
      });
    }
    if (node['@graph']) walk(node['@graph']);
  };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { walk(JSON.parse(s.textContent)); } catch (e) {}
  });
  ['meta[itemprop="price"]', 'meta[property="product:price:amount"]', 'meta[property="og:price:amount"]']
    .forEach(sel => document.querySelectorAll(sel).forEach(m => m.content && out.prices.push(m.content)));
  if (!out.prices.length) {
    // Stores that only show the price on screen (Target: data-test="product-price")
    const priceEl = document.querySelector(
      '[data-test="product-price"], [itemprop="price"], [data-testid="customer-price"], ' +
      '.priceView-customer-price, [data-automation-id="product-price"], [data-testid="price"]');
    const m = priceEl && (priceEl.innerText || '').match(/\$\s?([\d,]+\.?\d*)/);
    if (m) out.prices.push(m[1]);
  }
  document.querySelectorAll('button, input[type="submit"], a[role="button"]').forEach(b => {
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const text = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().toLowerCase();
    if (!text || text.length > 60) return;
    const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
    out.buttons.push({text, disabled});
  });
  out.title = document.title || '';
  out.text = (document.body ? document.body.innerText : '').toLowerCase().slice(0, 20000);
  return out;
}
"""


def interpret(info, item):
    """Turn what we read off the page into ('in_stock' | 'out_of_stock' | 'blocked' | 'unknown', price)."""
    head = (info.get("title", "") + " " + info.get("text", "")[:3000]).lower()
    if any(marker in head for marker in BLOCK_MARKERS):
        return "blocked", None

    prices = [p for p in (to_float(x) for x in info.get("prices", [])) if p and p > 0]
    price = min(prices) if prices else None

    custom_in = [p.lower() for p in item.get("in_stock_phrases", []) or []]
    custom_out = [p.lower() for p in item.get("out_of_stock_phrases", []) or []]
    in_words = custom_in or DEFAULT_IN_STOCK
    out_words = custom_out or DEFAULT_OUT_OF_STOCK

    # 1) Structured stock status the store publishes (most shops: Walmart, Shopify sneaker/game stores, etc.)
    #    Skipped if the item has its own phrases, so those always win.
    if not (custom_in or custom_out):
        avail = " ".join(info.get("availability", [])).lower()
        if any(k in avail for k in ("instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale")):
            return "in_stock", price
        if any(k in avail for k in ("outofstock", "soldout", "discontinued")):
            return "out_of_stock", price

    # 2) The first buy-style button on the page (the main one comes before "you may also like" carousels)
    for button in info.get("buttons", []):
        text = button["text"]
        if any(w in text for w in out_words):
            return "out_of_stock", price
        if any(w in text for w in in_words):
            return ("out_of_stock" if button["disabled"] else "in_stock"), price

    # 3) Plain page text as a last resort
    text = info.get("text", "")
    if any(w in text for w in out_words):
        return "out_of_stock", price
    if any(w in text for w in in_words):
        return "in_stock", price
    return "unknown", price


DEBUG = False


# Runs inside the page: true once a buy/sold-out style button (or a bot check) is showing.
WAIT_SCRIPT = r"""
(phrases) => {
  if (/robot or human|access denied|captcha|are you a human/i.test(document.title)) return true;
  return [...document.querySelectorAll('button, input[type="submit"], a[role="button"]')].some(b => {
    const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().toLowerCase();
    return t && t.length <= 60 && phrases.some(p => t.includes(p));
  });
}
"""


# --- Target: read the stock data the page downloads from Target's own servers -------------
TARGET_IN = ("IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE", "BACKORDER")
TARGET_OUT = ("OUT_OF_STOCK", "UNAVAILABLE", "PRE_ORDER_UNSELLABLE", "NOT_SOLD_IN_STORE", "DISCONTINUED")


def target_tcin(url):
    """Target's item number: the preselected version if there is one, else the A-number."""
    m = re.search(r"[?&]preselect=(\d+)", url) or re.search(r"/A-(\d+)", url)
    return m.group(1) if m else None


def _walk(node, found):
    if isinstance(node, dict):
        ship = node.get("shipping_options")
        if isinstance(ship, dict) and ship.get("availability_status"):
            found["shipping"].append(str(ship["availability_status"]).upper())
        for key in ("current_retail", "current_retail_min"):
            value = to_float(node.get(key))
            if value:
                found["prices"].append(value)
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)


def target_status(responses, tcin):
    """(status, price) from Target's background data, or (None, None) if nothing usable yet."""
    matching = [body for url, body in responses if tcin and tcin in url]
    chosen = matching or [body for _, body in responses]
    found = {"shipping": [], "prices": []}
    for body in chosen:
        _walk(body, found)
    if not found["prices"]:  # the price often comes in a separate download (e.g. for the main listing)
        everything = {"shipping": [], "prices": []}
        for _, body in responses:
            _walk(body, everything)
        found["prices"] = everything["prices"]
    price = found["prices"][0] if found["prices"] else None
    for status in found["shipping"]:
        if status in TARGET_IN:
            return "in_stock", price
    for status in found["shipping"]:
        if status in TARGET_OUT:
            return "out_of_stock", price
    return None, price


# --- Best Buy: official developer API (free key from developer.bestbuy.com) ----------------
def bestbuy_sku(item):
    if item.get("sku"):
        return str(item["sku"]).strip()
    m = re.search(r"[?&]skuId=(\d+)", item["url"]) or re.search(r"/(\d{6,8})\.p", item["url"])
    return m.group(1) if m else None


def check_bestbuy(item):
    """(status, price) from Best Buy's API, or None if it can't be used for this item."""
    key, sku = os.environ.get("BESTBUY_API_KEY", "").strip(), bestbuy_sku(item)
    if not (key and sku):
        if DEBUG:
            log(f"  [debug] Best Buy API not used (needs BESTBUY_API_KEY and a sku: — have key: {bool(key)}, sku: {sku})")
        return None
    try:
        r = requests.get(
            f"https://api.bestbuy.com/v1/products(sku={sku})",
            params={"apiKey": key, "format": "json",
                    "show": "sku,name,salePrice,regularPrice,onlineAvailability,orderable"},
            timeout=20,
        )
        r.raise_for_status()
        products = r.json().get("products", [])
    except Exception as e:  # noqa: BLE001
        log(f"  Best Buy API error: {e}")
        return "unknown", None
    if not products:
        log(f"  Best Buy API: no product with SKU {sku} — check the sku: for this item")
        return "unknown", None
    product = products[0]
    if DEBUG:
        log(f"  [debug] Best Buy API: {product}")
    status = "in_stock" if product.get("onlineAvailability") else "out_of_stock"
    return status, to_float(product.get("salePrice"))


CLICK_READY_SCRIPT = r"""
(word) => [...document.querySelectorAll('button')].some(b =>
  (b.innerText || '').trim().toLowerCase().startsWith(word))
"""


def check_item(page, item, number=0):
    if store_name(item["url"]) == "Best Buy":
        result = check_bestbuy(item)
        if result:
            return result
    custom = [p.lower() for p in (item.get("in_stock_phrases") or []) + (item.get("out_of_stock_phrases") or [])]
    phrases = custom or (DEFAULT_IN_STOCK + DEFAULT_OUT_OF_STOCK)
    is_target = store_name(item["url"]) == "Target" and not item.get("in_stock_phrases")
    responses = []

    raw, parsed = [], {}

    def grab(response):  # just remember it; it's read later (reading here would hang Playwright)
        if "redsky" in response.url and "json" in response.headers.get("content-type", ""):
            raw.append(response)

    def collect():
        for response in raw:
            if id(response) not in parsed:
                try:
                    parsed[id(response)] = (response.url, response.json())
                except Exception:  # noqa: BLE001
                    parsed[id(response)] = (response.url, None)
        responses[:] = [v for v in parsed.values() if v[1] is not None]

    if is_target:
        page.on("response", grab)
    try:
        page.goto(item["url"], timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        if is_target:
            tcin = target_tcin(item["url"])
            for _ in range(30):  # up to ~15 seconds for Target's stock data to arrive
                collect()
                status, price = target_status(responses, tcin)
                if status:
                    break
                page.wait_for_timeout(500)
            if DEBUG:
                dump = ROOT / f"debug-{number}.json"
                dump.write_text(json.dumps([{"url": u, "data": b} for u, b in responses], indent=1)[:2_000_000])
                for u, _ in responses:
                    log(f"  [debug] Target data: {re.sub(r'key=[^&]+', 'key=…', u.split('redsky_aggregations/v1/')[-1])[:110]}")
                log(f"  [debug] Target data saved to {dump.name}")
                log(f"  [debug] Target item #{tcin}: stock data says {status or 'nothing yet'}, "
                    f"price {money(price)} ({len(responses)} data responses seen)")
            if status:
                return status, price
            if any(m in (page.title() or "").lower() for m in BLOCK_MARKERS):
                return "blocked", None
        # Target only shows "Add to cart" once Pickup/Delivery/Shipping is selected, and the
        # bot's assigned store is random, so pick Shipping (nationwide) by default.
        # Any item can set `click: "Some button text"` to click a different option first.
        click_text = item.get("click") or ("shipping" if store_name(item["url"]) == "Target" else None)
        if click_text:
            try:
                page.wait_for_function(CLICK_READY_SCRIPT, arg=click_text.lower(), timeout=10000)
                page.locator("button").filter(
                    has_text=re.compile(r"^\s*" + re.escape(click_text), re.I)).first.click(timeout=5000)
                if DEBUG:
                    log(f"  [debug] clicked the {click_text!r} option")
            except Exception:  # noqa: BLE001
                if DEBUG:
                    log(f"  [debug] couldn't find a {click_text!r} option to click")
        try:
            # Many stores (Target especially) load the Add to cart button a few seconds
            # after the rest of the page, so wait for it — up to wait_seconds (default 15).
            page.wait_for_function(WAIT_SCRIPT, arg=phrases, timeout=int(item.get("wait_seconds", 15)) * 1000)
        except Exception:  # noqa: BLE001 — no button showed up; read what's there
            pass
        page.wait_for_timeout(1000)
        info = page.evaluate(PAGE_SCRIPT)
        if DEBUG:
            log(f"  [debug] page title: {info.get('title', '')[:100]}")
            log(f"  [debug] stock data published by store: {info.get('availability') or 'none'}")
            log(f"  [debug] prices found: {info.get('prices')[:5] or 'none'}")
            words = phrases + WEAK_OUT_OF_STOCK
            matches = [b for b in info.get("buttons", []) if any(w in b["text"] for w in words)]
            log("  [debug] buttons that look like buy / sold-out buttons, in page order:")
            for b in matches[:10] or [{"text": "(none found)", "disabled": False}]:
                log(f"      {'(disabled) ' if b['disabled'] else ''}{b['text']!r}")
            text = info.get("text", "")
            for w in words:
                i = text.find(w)
                if i >= 0:
                    snippet = text[max(0, i - 40): i + 40].replace("\n", " ")
                    log(f"  [debug] page text has {w!r}: ...{snippet}...")
            shot = ROOT / f"debug-{number}.png"
            page.screenshot(path=str(shot))
            log(f"  [debug] screenshot of what the bot saw: {shot.name}")
    except Exception as e:  # noqa: BLE001
        log(f"  could not load page: {str(e).splitlines()[0][:150]}")
        return "unknown", None
    finally:
        if is_target:
            page.remove_listener("response", grab)
    status, price = interpret(info, item)
    if is_target and price is None:
        collect()
        price = target_status(responses, target_tcin(item["url"]))[1]
    return status, price


# ---------------------------------------------------------------------------
# eBay price comparison (optional — only runs if EBAY_CLIENT_ID/SECRET are set)
# ---------------------------------------------------------------------------

_ebay_token = None


def ebay_prices(item):
    """Lowest and median asking price for new, buy-it-now eBay listings. None if not set up."""
    global _ebay_token
    client_id, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not (client_id and secret):
        return None
    try:
        if not _ebay_token:
            r = requests.post(
                "https://api.ebay.com/identity/v1/oauth2/token",
                auth=(client_id, secret),
                data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
                timeout=15,
            )
            r.raise_for_status()
            _ebay_token = r.json()["access_token"]
        query = item.get("ebay_search") or item["name"]
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {_ebay_token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            params={"q": query, "limit": 50, "filter": "buyingOptions:{FIXED_PRICE},conditionIds:{1000}"},
            timeout=15,
        )
        r.raise_for_status()
        prices = []
        for listing in r.json().get("itemSummaries", []):
            value = to_float(listing.get("price", {}).get("value"))
            if value and listing.get("price", {}).get("currency", "USD") == "USD":
                prices.append(value)
        msrp = to_float(item.get("msrp"))
        if msrp:  # drop single cards / empty boxes / accessories that match the search words
            prices = [p for p in prices if p >= msrp * 0.5]
        if len(prices) < 3:
            return None
        return {"low": min(prices), "median": statistics.median(prices), "count": len(prices),
                "search": f"https://www.ebay.com/sch/i.html?_nkw={requests.utils.quote(query)}&LH_BIN=1&LH_ItemCondition=1000"}
    except Exception as e:  # noqa: BLE001
        log(f"  eBay lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord(content=None, embed=None):
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url.startswith("https://"):
        log("  (no DISCORD_WEBHOOK_URL set — would have sent: "
            f"{(embed or {}).get('title') or content})")
        return False
    payload = {"allowed_mentions": {"parse": ["everyone", "roles", "users"]}}
    if content:
        payload["content"] = content
    if embed:
        embed.setdefault("timestamp", now_iso())
        payload["embeds"] = [embed]
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 429:  # Discord rate limit: wait and retry
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log(f"  Discord send failed: {e}")
            time.sleep(2)
    return False


def price_fields(item, price):
    fields = []
    msrp = to_float(item.get("msrp"))
    store_line = money(price)
    if price and msrp:
        diff = price - msrp
        if diff > msrp * 0.05:
            store_line += f"  ⚠️ {money(diff)} over MSRP — could be a third-party seller"
        elif diff < -0.5:
            store_line += f"  🔥 {money(-diff)} under MSRP"
        else:
            store_line += "  ✅ at MSRP"
    fields.append({"name": "Store price", "value": store_line, "inline": False})
    if msrp:
        fields.append({"name": "MSRP", "value": money(msrp), "inline": True})
    ebay = ebay_prices(item)
    if ebay:
        fields.append({
            "name": "eBay (new, buy it now)",
            "value": f"Lowest {money(ebay['low'])} · median {money(ebay['median'])} "
                     f"from {ebay['count']} listings · [see listings]({ebay['search']})",
            "inline": False,
        })
    return fields


def alert_restock(item, price):
    store = store_name(item["url"])
    embed = {
        "title": f"🟢 {item['name']}",
        "url": item["url"],
        "description": f"**Back in stock at {store}!**\n[👉 Open the product page]({item['url']})",
        "color": GREEN,
        "fields": price_fields(item, price),
        "footer": {"text": f"Added by {item['added_by']}" if item.get("added_by") else "Restock Bot"},
    }
    mention = item.get("mention", "@here")
    send_discord(content=f"{mention} 🚨 **RESTOCK:** {item['name']} ({store})".strip(), embed=embed)


def announce_new_item(item, status, price):
    store = store_name(item["url"])
    words = {
        "in_stock": "🟢 In stock right now",
        "out_of_stock": "🔴 Out of stock — you'll get pinged when it comes back",
        "blocked": f"🚫 {store} showed a bot check instead of the page, so this one can't be watched from here for now"
                   + (" — add a free Best Buy API key and the item's `sku:` to fix this (see README)" if store == "Best Buy" else ""),
        "unknown": ("❔ Best Buy blocked the page. Add a free Best Buy API key and the item's `sku:` to fix this (see README)"
                    if store == "Best Buy" else
                    f"❔ Couldn't tell if it's in stock — {store} may be limiting what the bot can see"),
    }
    send_discord(embed={
        "title": f"👀 Now watching: {item['name']}",
        "url": item["url"],
        "description": f"{store} · {words[status]}\nCurrent price: {money(price)}"
                       + (f" · MSRP {money(to_float(item.get('msrp')))}" if item.get("msrp") else "")
                       + f"\n[👉 Open the product page]({item['url']})",
        "color": BLUE,
    })


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(page, state):
    items, complete = load_watchlist(state)
    log(f"Checking {len(items)} item(s)...")
    seen = set()

    for item in items:
        key = item_key(item)
        seen.add(key)
        status, price = check_item(page, item, len(seen))
        record = state["items"].get(key)
        is_new = record is None
        record = record or {"name": item["name"], "buyable": None, "fails": 0}
        record["name"] = item["name"]

        max_price = to_float(item.get("max_price"))
        price_ok = max_price is None or price is None or price <= max_price
        log(f"  {item['name']} [{store_name(item['url'])}]: {status} {money(price) if price else ''}")

        if status in ("in_stock", "out_of_stock"):
            buyable = status == "in_stock" and price_ok
            if record.get("buyable") is False and buyable:
                alert_restock(item, price)
            if record.get("buyable") is not None and buyable != record.get("buyable"):
                record["changed"] = now_iso()
            record.update(status=status, buyable=buyable, fails=0)
            if price:
                record["price"] = price
        else:
            record["status"] = status
            fails = record.get("fails", 0) + 1
            if fails == BLOCKED_WARN_AFTER and not is_new:
                send_discord(embed={
                    "title": f"⚠️ Can't read: {item['name']}",
                    "url": item["url"],
                    "description": f"{store_name(item['url'])} has returned "
                                   f"{'a bot check' if status == 'blocked' else 'an unreadable page'} "
                                   f"for the last {BLOCKED_WARN_AFTER} checks, so restocks may be missed. "
                                   "I'll keep trying and stay quiet about it.",
                    "color": YELLOW,
                })
            record["fails"] = min(fails, BLOCKED_WARN_AFTER)  # stop counting so state.json stays unchanged

        if is_new:
            announce_new_item(item, status, price)
        state["items"][key] = record

    # Forget items that were removed from the watchlist (only if every source was read OK,
    # so a Google hiccup doesn't make the bot forget everything and re-announce it)
    if complete:
        for key in list(state["items"]):
            if key not in seen:
                del state["items"][key]
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Restock alert bot")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="keep running, checking every SECONDS")
    parser.add_argument("--test", action="store_true", help="send a test alert to Discord and exit")
    parser.add_argument("--debug", action="store_true", help="print what the bot sees on each page")
    args = parser.parse_args()
    global DEBUG
    DEBUG = args.debug
    load_dotenv()

    if args.test:
        state = {"items": {}}
        items, _ = load_watchlist(state)
        sample = items[0] if items else {"name": "Test item", "url": "https://example.com"}
        ok = send_discord(
            content="🧪 Test alert — if you can see this, the bot can reach Discord.",
            embed={"title": f"🟢 {sample['name']} (TEST)", "url": sample["url"],
                   "description": f"This is what a restock alert looks like.\n[👉 Open the product page]({sample['url']})",
                   "color": GREEN, "fields": price_fields(sample, to_float(sample.get("msrp")))},
        )
        log("Test sent." if ok else "Test NOT sent — check DISCORD_WEBHOOK_URL.")
        sys.exit(0 if ok else 1)

    state = load_state()
    state.setdefault("items", {})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        try:
            while True:
                run_once(page, state)
                if not args.loop:
                    break
                time.sleep(args.loop)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
