#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrapes Mike Ball 'All Expeditions' with Playwright (headless Chromium).
- Checks 'Hide unavailable expeditions'
- Clicks Search
- Waits for network to go idle
- Parses the results table by headers 'Departs' & 'Returns'
- Filters to next 6 months and skips SOLD OUT
- Writes mikeball_availability.json
"""

import json, re, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
TZ  = ZoneInfo("Australia/Sydney")

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_money_aud(text: str):
    if not text: return None
    m = re.search(r"([0-9][0-9,]*(?:\.\d{2})?)", text.replace(",", ""))
    return int(round(float(m.group(1)))) if m else None

def parse_date(text: str):
    if not text: return None
    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None

def within_next_six_months(d):
    if not d: return False
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    return now <= d <= six

def get_full_html():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 2200})
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        # small settle
        page.wait_for_timeout(1200)

        # Tick 'Hide unavailable expeditions'
        try:
            page.get_by_role("checkbox", name=re.compile(r"hide.*unavailable", re.I)).check()
        except Exception:
            pass

        # Click Search
        try:
            page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=2500)
        except Exception:
            pass

        # Wait until XHRs settle
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            page.wait_for_timeout(1500)

        # Try expanding any 'See more' buttons (to reveal cabin tables)
        try:
            for btn in page.locator("text=/^\\s*See\\s*more\\s*$/i").all():
                try: btn.click(timeout=800)
                except Exception: pass
            page.wait_for_timeout(600)
        except Exception:
            pass

        html = page.content()
        browser.close()
        return html

def parse_results(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # Find a table that has BOTH Departs and Returns in the header
    target = None
    for tbl in soup.find_all("table"):
        heads = [clean(th.get_text()) for th in tbl.find_all("th")]
        hj = " | ".join(heads).lower()
        if "depart" in hj and "return" in hj:
            target = tbl
            break
    if not target:
        return []

    trips = []
    for tr in target.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        row_text = clean(tr.get_text())
        if "sold out" in row_text.lower():
            continue

        cols = [clean(td.get_text()) for td in tds]
        # Expect approx: [Expeditions, Departs, Returns, Price From (AUD), Availability, ...]
        if len(cols) < 4:
            continue

        expedition = cols[0]
        departs_txt = cols[1] if len(cols) >= 2 else ""
        returns_txt = cols[2] if len(cols) >= 3 else ""
        price_txt   = cols[3] if len(cols) >= 4 else ""
        avail_txt   = cols[4] if len(cols) >= 5 else ""

        dep = parse_date(departs_txt)
        ret = parse_date(returns_txt)
        if not dep or not within_next_six_months(dep):
            continue

        # Cabin breakdown often sits in the next sibling row as a nested table
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

    trips.sort(key=lambda x: x["departs"] or "9999-12-31")
    return trips

def main():
    html = get_full_html()
    trips = parse_results(html)
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    payload = {
        "source_url": URL,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "window_start": now.isoformat(),
        "window_end": six.isoformat(),
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
