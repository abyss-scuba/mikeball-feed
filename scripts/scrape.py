#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Mike Ball "All Expeditions" with a headless browser (Playwright),
because the availability grid is rendered by JavaScript.

- Filters to the next 6 months
- Skips any expedition marked SOLD OUT (or not shown when "Hide unavailable" is active)
- Extracts top row fields + expanded "Cabin Type" rows (if visible)
- Writes mikeball_availability.json at repo root

Output shape:
{
  "source_url": "...",
  "generated_at": "...",
  "window_start": "YYYY-MM-DD",
  "window_end": "YYYY-MM-DD",
  "trips": [
    {
      "expedition": "...",
      "departs": "YYYY-MM-DD",
      "returns": "YYYY-MM-DD",
      "price_from_aud": 2385,
      "availability": "Hurry" | "Available" | "Few left" | "6 available" | "...",
      "cabins": [
        {"cabin_type":"Premium", "berths_left": 2, "price_aud": 3404},
        ...
      ]
    }
  ]
}
"""

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright

SOURCE_URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
TZ = ZoneInfo("Australia/Sydney")

# ------------ helpers ------------
def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_money_aud(text: str):
    if not text:
        return None
    m = re.search(r"([0-9][0-9,]*(?:\.\d{2})?)", text.replace(",", ""))
    if not m:
        return None
    try:
        # Round to whole dollars
        return int(round(float(m.group(1))))
    except Exception:
        return None

def parse_date(text: str):
    if not text:
        return None
    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None

def within_next_six_months(d):
    if not d:
        return False
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    return now <= d <= six

def availability_label(txts):
    t = " ".join(txts).lower()
    if "sold out" in t:
        return "Sold Out"
    if "hurry" in t or "few" in t:
        return "Hurry"
    if "available" in t or "spaces" in t or "left" in t:
        # Try number
        m = re.search(r"(\d+)\s*(?:spaces?|left|avail)", t)
        return f"{m.group(1)} available" if m else "Available"
    return clean(" ".join(txts))[:50] or None

# ------------ scraping ------------
def get_rendered_html():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1280, "height": 2000})
        # Load page and give scripts time to populate
        page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # If a "Search" button exists, click it to ensure results render
        try:
            page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=2000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Expand “See more” (so Cabin table appears)
        try:
            for btn in page.locator("text=/^\\s*See\\s*more\\s*$/i").all():
                try:
                    btn.click(timeout=1000)
                except Exception:
                    pass
            page.wait_for_timeout(800)
        except Exception:
            pass

        html = page.content()
        browser.close()
        return html

def parse_html(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # Find the main table by header labels (works even if table caption text changes)
    candidate_tables = soup.find_all("table")
    target = None
    for tbl in candidate_tables:
        heads = [clean(th.get_text()) for th in tbl.find_all("th")]
        head_join = " | ".join(heads).lower()
        if ("expedition" in head_join or "expeditions" in head_join) and \
           ("depart" in head_join) and ("return" in head_join) and \
           ("price" in head_join) and ("avail" in head_join):
            target = tbl
            break
    # Fallback: the first table that has Departs/Returns
    if not target:
        for tbl in candidate_tables:
            heads = [clean(th.get_text()) for th in tbl.find_all("th")]
            head_join = " | ".join(heads).lower()
            if ("depart" in head_join) and ("return" in head_join):
                target = tbl
                break

    trips = []
    if not target:
        return trips  # nothing found

    for tr in target.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        row_text = clean(tr.get_text())
        if "sold out" in row_text.lower():
            continue  # skip unavailable

        # Heuristic column mapping: Title | Departs | Returns | Price From (AUD) | Availability
        cols = [clean(td.get_text()) for td in tds]
        if len(cols) < 4:
            continue

        # pick likely positions
        expedition = cols[0]
        departs_txt = cols[1] if len(cols) >= 2 else ""
        returns_txt = cols[2] if len(cols) >= 3 else ""
        price_txt   = cols[3] if len(cols) >= 4 else ""
        avail_txt   = cols[4] if len(cols) >= 5 else ""

        dep_date = parse_date(departs_txt)
        ret_date = parse_date(returns_txt)

        if not dep_date or not within_next_six_months(dep_date):
            continue

        # Cabin sub-table may be in the following sibling rows or inside this row
        cabins = []
        # Look for an inner table with "Cabin Type" header near this row
        inner_tbl = tr.find_next_sibling("tr")
        if inner_tbl and inner_tbl.find("th", string=re.compile(r"cabin type", re.I)):
            # It’s a cabin breakdown table. Parse its rows.
            for cab_tr in inner_tbl.find_all("tr"):
                ctd = [clean(x.get_text()) for x in cab_tr.find_all("td")]
                if len(ctd) >= 2:
                    ctype = ctd[0]
                    # find berths and price in any of the next cells
                    berths = None
                    for cell in ctd[1:]:
                        m = re.search(r"(\d+)\s*(?:berths?|left|avail)", cell.lower())
                        if m:
                            berths = int(m.group(1))
                    price_aud = None
                    for cell in ctd[1:]:
                        p = parse_money_aud(cell)
                        if p:
                            price_aud = p
                    if ctype:
                        cabins.append({
                            "cabin_type": ctype,
                            "berths_left": berths,
                            "price_aud": price_aud
                        })

        price_from_aud = parse_money_aud(price_txt)
        availability = availability_label([avail_txt, row_text])

        trips.append({
            "expedition": expedition or None,
            "departs": dep_date.isoformat(),
            "returns": ret_date.isoformat() if ret_date else None,
            "price_from_aud": price_from_aud,
            "availability": availability,
            "cabins": cabins
        })

    # Sort by departs
    trips.sort(key=lambda x: x["departs"] or "9999-12-31")
    return trips

def main():
    html = get_rendered_html()
    trips = parse_html(html)

    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)

    payload = {
        "source_url": SOURCE_URL,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "window_start": now.isoformat(),
        "window_end": six.isoformat(),
        "trips": trips
    }
    with open("mikeball_availability.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"✅ Wrote mikeball_availability.json with {len(trips)} trips in the next 6 months.")

if __name__ == "__main__":
    main()
