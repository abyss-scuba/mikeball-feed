#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Robust Mike Ball availability scraper via WordPress admin-ajax.

What this script does now:
- Loads the page, extracts ajaxurl (and unescapes it), and finds RescoApi JS files
- Extracts EVERY candidate 'action' string from the JS
- Tries many combinations:
    * each action (excluding obvious non-search ones like expand/berth/cabin)
    * date formats: "Friday 12 September 2025" AND "2025-09-12"
    * expedition: name='all' AND name=''
- Adds realistic Ajax headers (Referer, X-Requested-With)
- For each try, if the response contains a table with "Departs" OR "Returns", we stop and parse
- Writes two debug files:
    mikeball_actions_debug.txt  -> list of actions found and every attempt result
    mikeball_results_debug.html -> last server response body (for inspection)
- Outputs mikeball_availability.json (trips in next 6 months; SOLD OUT excluded)
"""

import json, re, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from bs4 import BeautifulSoup
import requests

URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
TZ  = ZoneInfo("Australia/Sydney")

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Sensible fallbacks we’ll also try if JS discovery misses the real one
LIKELY_ACTIONS = [
    "resco_search_availability",
    "ra_search_availability",
    "resco_availability",
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
    return d.strftime("%A %d %B %Y")  # e.g., Friday 12 September 2025

def iso_date(d):
    return d.strftime("%Y-%m-%d")

def fetch_main():
    r = requests.get(URL, headers=BASE_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

def extract_ajaxurl_and_js(html: str):
    m = re.search(r'rescoAjax\s*=\s*\{\s*"ajaxurl"\s*:\s*"([^"]+admin-ajax\.php)"', html)
    ajaxurl = m.group(1) if m else "https://www.mikeball.com/wp-admin/admin-ajax.php"
    ajaxurl = ajaxurl.replace("\\/", "/")  # unescape
    js_urls = re.findall(r'https?://[^"\']+/wp-content/plugins/RescoApi/assets/js/[^"\']+\.js', html)
    return ajaxurl, js_urls

def discover_actions_from_js(js_urls):
    actions = []
    for js_url in js_urls:
        try:
            r = requests.get(js_url, headers=BASE_HEADERS, timeout=30)
            if r.status_code != 200: continue
            js = r.text or ""
            # data: { action: 'xyz' }
            actions += [m.group(1) for m in re.finditer(r"action\s*:\s*['\"]([a-zA-Z0-9_:-]+)['\"]", js)]
            # Also pick action=xyz in querystrings
            actions += [m.group(1) for m in re.finditer(r"action=([a-zA-Z0-9_:-]+)", js)]
        except Exception:
            continue
    # uniquify, keep order
    seen, uniq = set(), []
    for a in actions:
        if a not in seen:
            seen.add(a); uniq.append(a)
    return uniq

def is_searchy(action: str):
    if not action: return False
    a = action.lower()
    if "expand" in a or "berth" in a or "cabin" in a:
        return False
    return ("search" in a) or ("avail" in a)  # prefer obvious ones

def try_post(ajaxurl, action, starts_at, ends_at, name_value, debug_lines):
    headers = dict(BASE_HEADERS)
    headers["Referer"] = URL
    headers["Origin"]  = "https://www.mikeball.com"
    headers["X-Requested-With"] = "XMLHttpRequest"

    data = {
        "action": action,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "name": name_value,          # '' or 'all'
        "hide_unavailable": "1",
    }
    try:
        r = requests.post(ajaxurl, headers=headers, data=data, timeout=40)
        text = r.text or ""
    except Exception as e:
        debug_lines.append(f"POST error action={action} dates=({starts_at}|{ends_at}) name={name_value} -> EXC {e}")
        return ("", False, debug_lines)

    ok = (r.status_code == 200 and "<table" in text and ("Departs" in text or "Returns" in text))
    debug_lines.append(f"POST {r.status_code} action={action} dates=({starts_at}|{ends_at}) name={name_value} -> {'MATCH' if ok else 'no match'} (len={len(text)})")

    # Always write last response so we can inspect if needed
    with open("mikeball_results_debug.html", "w", encoding="utf-8") as f:
        f.write(text)

    return (text, ok, debug_lines)

def fetch_results_html():
    html = fetch_main()
    ajaxurl, js_urls = extract_ajaxurl_and_js(html)
    log(f"ajaxurl: {ajaxurl}")
    if js_urls:
        log(f"plugin js: {', '.join(js_urls[:2])}{' ...' if len(js_urls)>2 else ''}")

    # Build candidates list: prefer “searchy” ones first, then everything else (minus expand/berth/cabin), then likely fallbacks
    discovered = discover_actions_from_js(js_urls)
    prefer = [a for a in discovered if is_searchy(a)]
    others = [a for a in discovered if (a not in prefer and not re.search(r"(expand|berth|cabin)", a, re.I))]
    candidates = prefer + others + [a for a in LIKELY_ACTIONS if a not in discovered]

    # Prepare parameter variants
    today = datetime.now(TZ).date()
    ends  = today + timedelta(days=31*6)
    date_variants = [(long_date(today), long_date(ends)), (iso_date(today), iso_date(ends))]
    name_variants = ["all", ""]  # some handlers treat empty as "all"

    debug_lines = []
    if discovered:
        debug_lines.append("== Discovered actions from JS ==")
        debug_lines += discovered
    else:
        debug_lines.append("== No actions discovered from JS ==")
    debug_lines.append("== Try order ==")
    debug_lines += [f"- {a}" for a in candidates]

    # Try all combinations until one matches
    for action in candidates:
        for (s, e) in date_variants:
            for name_val in name_variants:
                body, ok, debug_lines = try_post(ajaxurl, action, s, e, name_val, debug_lines)
                if ok:
                    with open("mikeball_actions_debug.txt", "w", encoding="utf-8") as f:
                        f.write("\n".join(debug_lines))
                    return body, today, ends

    # none matched
    with open("mikeball_actions_debug.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(debug_lines))
    return "", today, ends

def parse_results(results_html: str):
    if not results_html:
        return []

    soup = BeautifulSoup(results_html, "html.parser")

    # Find the first meaningful table (has Departs & Returns)
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

        # Cabin table often in the following <tr>
        cabins = []
        sib = tr.find_next_sibling("tr")
        if sib:
            cab_tbl = sib.find("table")
            if cab_tbl and cab_tbl.find("th", string=re.compile(r"cabin type", re.I)):
                for r in cab_tbl.find_all("tr"):
                    ctd = [clean(x.get_text()) for x in r.find_all("td")]
                    if not ctd: continue
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

    # de-dup + sort
    seen, out = set(), []
    for t in trips:
        key = (t["expedition"], t["departs"], t["returns"])
        if key not in seen:
            seen.add(key); out.append(t)
    out.sort(key=lambda x: x["departs"] or "9999-12-31")
    return out

def main():
    html, start_date, end_date = fetch_results_html()
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
