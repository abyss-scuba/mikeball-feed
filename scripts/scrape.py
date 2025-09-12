#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Mike Ball availability by calling the RescoApi Ajax endpoint directly.

Flow:
1) GET the page to learn ajaxurl and locate the plugin JS
2) Try to discover the Ajax 'action' name from the plugin JS
3) POST to admin-ajax.php with: starts_at, ends_at, name='all', hide_unavailable=1, action=...
4) Parse returned HTML table:
   - Skip SOLD OUT
   - Extract: expedition, departs, returns, price_from_aud, availability
   - Parse following 'Cabin Type' table when present (cabins: type, berths_left, price_aud)
5) Write mikeball_availability.json and mikeball_results_debug.html

Designed for GitHub Actions; no headless browser required.
"""

import json, re, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from bs4 import BeautifulSoup

import requests

URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
TZ  = ZoneInfo("Australia/Sydney")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

LIKELY_ACTION_NAMES = [
    # common patterns for WP AJAX handlers
    "resco_search_availability",
    "resco_availability",
    "ra_search_availability",
    "ra_availability",
    "search_availability",
    "availability_search",
    "get_availability",
]

def log(msg): print(f"[scraper] {msg}", flush=True)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_money_aud(text: str):
    if not text: return None
    m = re.search(r"([0-9][0-9,]*(?:\.\d{2})?)", text.replace(",", ""))
    try:
        return int(round(float(m.group(1)))) if m else None
    except Exception:
        return None

def parse_date(text: str):
    if not text: return None
    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None

def within_6m(d):
    if not d: return False
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    return now <= d <= six

def long_date(d):
    # Site uses long human format (e.g., Friday 12 September 2025)
    return d.strftime("%A %d %B %Y")

def get_ajaxurl_and_js():
    """Fetch the main page and extract ajaxurl and plugin JS URL(s)."""
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text

    # ajaxurl from inline var: var rescoAjax = {"ajaxurl":"https://.../wp-admin/admin-ajax.php"}
    m = re.search(r'rescoAjax\s*=\s*\{\s*"ajaxurl"\s*:\s*"([^"]+admin-ajax\.php)"', html)
    ajaxurl = m.group(1) if m else "https://www.mikeball.com/wp-admin/admin-ajax.php"

    # find plugin JS (RescoApi assets js)
    js_urls = re.findall(
        r'https?://[^"\']+/wp-content/plugins/RescoApi/assets/js/[^"\']+\.js', html
    )
    return ajaxurl, js_urls

def discover_action_from_js(js_urls):
    """Try to read the plugin JS and detect the Ajax 'action' name."""
    for js_url in js_urls:
        try:
            r = requests.get(js_url, headers=HEADERS, timeout=30)
            if r.status_code != 200 or not r.text:
                continue
            js = r.text

            # Search for typical jQuery ajax post patterns that include 'action'
            # e.g. data: { action: 'resco_search_availability', ... }
            m = re.search(r"action\s*:\s*['\"]([a-zA-Z0-9_:-]+)['\"]", js)
            if m:
                return m.group(1)

            # Another pattern: 'action=resco_search_availability' in query strings
            m = re.search(r"action=([a-zA-Z0-9_:-]+)", js)
            if m:
                return m.group(1)

        except Exception:
            continue
    return None

def try_post(ajaxurl, action, starts_at, ends_at):
    """POST to admin-ajax.php using a candidate action. Return HTML (or None)."""
    data = {
        "action": action,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "name": "all",                 # -- ALL EXPEDITIONS --
        "hide_unavailable": "1",       # checkbox checked
    }
    # WP admin-ajax typically expects POST
    r = requests.post(ajaxurl, headers=HEADERS, data=data, timeout=40)
    if r.status_code != 200:
        return None
    text = r.text or ""
    # Quick sanity check that we got a table-like payload back
    if "<table" in text and ("Departs" in text or "Returns" in text):
        return text
    return None

def fetch_results_html():
    """Main driver to obtain the rendered results HTML via Ajax POST."""
    ajaxurl, js_urls = get_ajaxurl_and_js()
    log(f"ajaxurl: {ajaxurl}")
    if js_urls:
        log(f"plugin js: {', '.join(js_urls[:2])}{' ...' if len(js_urls)>2 else ''}")

    today = datetime.now(TZ).date()
    ends  = today + timedelta(days=31*6)
    start_str = long_date(today)
    end_str   = long_date(ends)

    # 1) Try to discover the 'action' from plugin JS
    action = discover_action_from_js(js_urls)
    if action:
        log(f"discovered action in JS: {action}")
        html = try_post(ajaxurl, action, start_str, end_str)
        if html:
            return html, today, ends

    # 2) Try a short list of likely action names
    log("discovery failed; trying likely action names…")
    for candidate in LIKELY_ACTION_NAMES:
        log(f"trying action={candidate}")
        html = try_post(ajaxurl, candidate, start_str, end_str)
        if html:
            log(f"success with action={candidate}")
            return html, today, ends

    # 3) Give up (write empty results for now)
    log("failed to obtain results from Ajax endpoint")
    return "", today, ends

def parse_results(results_html: str):
    if not results_html:
        return []

    soup = BeautifulSoup(results_html, "html.parser")

    # Find the first table that has headers Departs & Returns
    table = None
    for tbl in soup.find_all("table"):
        heads = [clean(th.get_text()) for th in tbl.find_all("th")]
        hdr = " | ".join(heads).lower()
        if "depart" in hdr and "return" in hdr:
            table = tbl
            break
    if not table:
        return []

    trips = []
    rows = table.find_all("tr")

    for i, tr in enumerate(rows):
        tds = tr.find_all("td")
        if not tds:
            continue

        row_text = clean(tr.get_text()).lower()
        if "sold out" in row_text:
            continue

        cols = [clean(td.get_text()) for td in tds]
        if len(cols) < 4:
            continue

        expedition  = cols[0]
        departs_txt = cols[1] if len(cols) >= 2 else ""
        returns_txt = cols[2] if len(cols) >= 3 else ""
        price_txt   = cols[3] if len(cols) >= 4 else ""
        avail_txt   = cols[4] if len(cols) >= 5 else ""

        dep = parse_date(departs_txt)
        ret = parse_date(returns_txt)
        if not dep or not within_6m(dep):
            continue

        # Cabin breakdown is often in the next row as a nested table
        cabins = []
        if i + 1 < len(rows):
            sib = rows[i + 1]
            cab_tbl = sib.find("table")
            if cab_tbl and cab_tbl.find("th", string=re.compile(r"cabin type", re.I)):
                for r in cab_tbl.find_all("tr"):
                    ctd = [clean(x.get_text()) for x in r.find_all("td")]
                    if not ctd:
                        continue
                    ctype = ctd[0]
                    berths = None
                    price_aud = None
                    for cell in ctd[1:]:
                        m = re.search(r"(\d+)\s*(?:berths?|left|avail)", cell.lower())
                        if m: berths = int(m.group(1))
                        p = parse_money_aud(cell)
                        if p: price_aud = p
                    if ctype:
                        cabins.append({
                            "cabin_type": ctype,
                            "berths_left": berths,
                            "price_aud": price_aud
                        })

        trips.append({
            "expedition": expedition or None,
            "departs": dep.isoformat(),
            "returns": ret.isoformat() if ret else None,
            "price_from_aud": parse_money_aud(price_txt),
            "availability": avail_txt or None,
            "cabins": cabins
        })

    # De-dup & sort
    seen, out = set(), []
    for t in trips:
        key = (t["expedition"], t["departs"], t["returns"])
        if key not in seen:
            seen.add(key); out.append(t)
    out.sort(key=lambda x: x["departs"] or "9999-12-31")
    return out

def main():
    html, start_date, end_date = fetch_results_html()

    # Always save what we received for debugging/verification
    with open("mikeball_results_debug.html", "w", encoding="utf-8") as f:
        f.write(html or "")

    trips = parse_results(html)
    payload = {
        "source_url": URL,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "trips": trips
    }
    with open("mikeball_availability.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log(f"✅ wrote mikeball_availability.json with {len(trips)} trips")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL:", e, file=sys.stderr)
        sys.exit(1)
