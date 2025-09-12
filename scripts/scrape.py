#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, re, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
TZ  = ZoneInfo("Australia/Sydney")

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_money_aud(text: str):
    if not text: return None
    m = re.search(r"([0-9][0-9,]*(?:\.\d{2})?)", text.replace(",", ""))
    try: return int(round(float(m.group(1)))) if m else None
    except Exception: return None

def parse_date(text: str):
    if not text: return None
    try: return dateparser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception: return None

def within_6m(d):
    if not d: return False
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    return now <= d <= six

def long_date(d): return d.strftime("%A %d %B %Y")  # e.g. Friday 12 September 2025

def fetch_ajax_html():
    today = datetime.now(TZ).date()
    end   = today + timedelta(days=31*6)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1300, "height": 2200},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36")
        )

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800)

        # Fill Start/End
        try:
            page.fill("#starts_at", long_date(today))
            page.fill("#ends_at",   long_date(end))
        except Exception:
            pass

        # Select ALL EXPEDITIONS
        try:
            page.select_option("select[name='name']", value="all")
        except Exception:
            pass

        # Hide unavailable
        try:
            page.check("input[name='hide_unavailable']")
        except Exception:
            pass

        # Predicate for the admin-ajax response
        def is_availability_resp(resp):
            return ("admin-ajax.php" in resp.url) and (resp.request.method.upper() == "POST")

        # Click Search while *expecting* the response
        body = ""
        try:
            with page.expect_response(is_availability_resp, timeout=20000) as resp_info:
                # primary button used by plugin
                try:
                    page.click("button.ra-ajax", timeout=5000)
                except Exception:
                    # fallback by role/name
                    page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=3000)
            resp = resp_info.value
            body = resp.text()
        except PWTimeout:
            # last resort: wait for network idle and read results container if present
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
                # sometimes plugin also injects into #availability-results
                body = page.inner_html("#availability-results")
            except Exception:
                body = ""

        browser.close()

        # Always write a debug snapshot of what we got from admin-ajax
        with open("mikeball_results_debug.html", "w", encoding="utf-8") as f:
            f.write(body or "")
        return body, today, end

def parse_results(html: str):
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")

    # Find table that has Departs & Returns
    table = None
    for tbl in soup.find_all("table"):
        heads = [clean(th.get_text()) for th in tbl.find_all("th")]
        hdr = " | ".join(heads).lower()
        if "depart" in hdr and "return" in hdr:
            table = tbl; break
    if not table: return []

    trips = []
    rows = table.find_all("tr")

    for i, tr in enumerate(rows):
        tds = tr.find_all("td")
        if not tds: continue
        if "sold out" in clean(tr.get_text()).lower(): continue

        cols = [clean(td.get_text()) for td in tds]
        if len(cols) < 4: continue

        expedition  = cols[0]
        departs_txt = cols[1] if len(cols) >= 2 else ""
        returns_txt = cols[2] if len(cols) >= 3 else ""
        price_txt   = cols[3] if len(cols) >= 4 else ""
        avail_txt   = cols[4] if len(cols) >= 5 else ""

        dep = parse_date(departs_txt)
        ret = parse_date(returns_txt)
        if not dep or not within_6m(dep): continue

        # Cabin breakdown is usually the next row with nested table
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

    # de-dup & sort
    seen, out = set(), []
    for t in trips:
        key = (t["expedition"], t["departs"], t["returns"])
        if key not in seen:
            seen.add(key); out.append(t)
    out.sort(key=lambda x: x["departs"] or "9999-12-31")
    return out

def main():
    html, start_date, end_date = fetch_ajax_html()
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
    print(f"✅ wrote mikeball_availability.json with {len(trips)} trips")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL:", e, file=sys.stderr)
        sys.exit(1)
