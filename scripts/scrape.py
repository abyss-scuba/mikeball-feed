#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrapes Mike Ball "All Expeditions" with Playwright (headless Chromium).
- Renders JS (so the table exists)
- Ticks "Hide unavailable expeditions" and clicks Search
- Waits specifically for the table that has header "Price From (AUD)"
- Expands "See more" (to reveal cabin table)
- Extracts next 6 months, skipping SOLD OUT
- Fields: expedition, departs, returns, price_from_aud, availability, cabins[]
- Writes mikeball_availability.json (repo root)
"""

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
    # accept "AUD 2,385" or "$2,385"
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

def get_rendered_table_html():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 2000})
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        # give page scripts a moment
        page.wait_for_timeout(1200)

        # Tick "Hide unavailable expeditions" if present
        try:
            cb = page.get_by_role("checkbox", name=re.compile(r"hide.*unavailable", re.I))
            cb.check()
        except Exception:
            pass

        # Click "Search" to ensure results render
        try:
            page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=2500)
        except Exception:
            pass

        # Wait for the results table that has "Price From (AUD)" header
        # (this is the most stable hitching point on the page)
        try:
            page.wait_for_selector(
                "xpath=//table[.//th[contains(.,'Price From') and contains(.,'AUD')]]",
                timeout=20000
            )
        except PWTimeout:
            # small extra wait; some loads are slow
            page.wait_for_timeout(1500)

        # Expand "See more" buttons to reveal the cabin table, if present
        try:
            for btn in page.locator("text=/^\\s*See\\s*more\\s*$/i").all():
                try: btn.click(timeout=800)
                except Exception: pass
            page.wait_for_timeout(600)
        except Exception:
            pass

        # Grab the *outerHTML* of the price/availability table only
        table_el = page.locator(
            "xpath=//table[.//th[contains(.,'Price From') and contains(.,'AUD')]]"
        ).first
        outer = table_el.evaluate("el => el.outerHTML")
        browser.close()
        return outer

def parse_table_html(table_html: str):
    soup = BeautifulSoup(table_html, "html.parser")
    trips = []

    # header map (not strictly needed, but helpful if column order changes)
    headers = [clean(th.get_text()) for th in soup.find_all("th")]
    # We expect these headers somewhere across the thead:
    # "Expeditions", "Departs", "Returns", "Price From (AUD)", "Availability"

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:  # skip header rows
            continue

        row_text = clean(tr.get_text())
        if "sold out" in row_text.lower():
            continue

        cols = [clean(td.get_text()) for td in tds]
        # Heuristic: title, departs, returns, price, availability in the first 5 cols
        if len(cols) < 4:
            continue

        expedition = cols[0]
        departs_txt = cols[1] if len(cols) >= 2 else ""
        returns_txt = cols[2] if len(cols) >= 3 else ""
        price_txt   = cols[3] if len(cols) >= 4 else ""
        avail_txt   = cols[4] if len(cols) >= 5 else ""

        dep_date = parse_date(departs_txt)
        ret_date = parse_date(returns_txt)
        if not within_next_six_months(dep_date):
            continue

        # Cabin table often appears as the next <tr> (sibling) with its own <table>
        cabins = []
        sib = tr.find_next_sibling("tr")
        if sib:
            cab_table = sib.find("table")
            # Look for a cabin header
            if cab_table and cab_table.find("th", string=re.compile(r"cabin type", re.I)):
                for c_tr in cab_table.find_all("tr"):
                    ctd = [clean(x.get_text()) for x in c_tr.find_all("td")]
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
            "departs": dep_date.isoformat() if dep_date else None,
            "returns": ret_date.isoformat() if ret_date else None,
            "price_from_aud": parse_money_aud(price_txt),
            "availability": avail_txt or None,
            "cabins": cabins
        })

    trips.sort(key=lambda x: x["departs"] or "9999-12-31")
    return trips

def main():
    table_html = get_rendered_table_html()
    trips = parse_table_html(table_html)

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
