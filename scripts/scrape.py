#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape "All Expeditions" from:
https://www.mikeball.com/availability-mike-ball-dive-expeditions/

Outputs mikeball.json at repo root with shape:
{
  "source_url": "...",
  "generated_at": "ISO8601",
  "window_start": "YYYY-MM-DD",
  "window_end": "YYYY-MM-DD",
  "trips": [
    {
      "expedition": "7 Night Coral Sea Exploratory",
      "departs": "YYYY-MM-DD",
      "returns": "YYYY-MM-DD",
      "price_from_aud": 5367,
      "availability": "6 available" | "Hurry" | "Good" | "Limited" | "...",
      "cabins": [
        {"cabin_type":"Premium", "berths_left": 2, "price_aud": 6999}
      ]
    },
    ...
  ]
}

Notes:
- Skips rows containing SOLD OUT.
- Limits to next 6 months from 'today' in Australia/Sydney time.
- Tries to pick up cabin breakdown rows directly beneath each departure.
"""

import json
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

SOURCE_URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
TZ = ZoneInfo("Australia/Sydney")

# -------- helpers --------
def clean_text(x: str) -> str:
    return re.sub(r"\s+", " ", (x or "").strip())

def parse_money_aud(text: str):
    if not text:
        return None
    # strip non-digits except '.' and ','
    m = re.findall(r"[\$A-Za-z]*\s*([0-9][0-9,]*(?:\.[0-9]{2})?)", text)
    if not m:
        return None
    try:
        return int(float(m[0].replace(",", "")))
    except Exception:
        return None

def parse_date(text: str):
    if not text:
        return None
    # Mike Ball often uses e.g. "11 Sep 2025"
    try:
        dt = dateparser.parse(text, dayfirst=True, fuzzy=True)
        return dt.date()
    except Exception:
        return None

def within_next_six_months(d):
    if not d:
        return False
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    return now <= d <= six

def looks_like_date(s):
    return bool(re.search(r"\b\d{1,2}\s+\w{3,}\s+\d{4}\b", s))

def guess_availability(texts):
    # Look for common flags on Mike Ball site: "Hurry", "Good", "Limited", "Few", numbers, etc.
    joined = " ".join(texts).lower()
    for key in ["sold out", "hurry", "good", "limited", "few", "waitlist"]:
        if key in joined:
            return key.title()
    # numbers like "6 left", "6 available"
    m = re.search(r"(\d+)\s*(?:left|available|spaces?)", joined)
    if m:
        return f"{m.group(1)} available"
    return clean_text(" ".join(texts))[:80] or None

# -------- scrape --------
def fetch_html(url=SOURCE_URL):
    headers = {
        "User-Agent": "AbyssSpoilsportBot/1.0 (+https://www.abyss.com.au/)"
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def find_all_expeditions_table(soup: BeautifulSoup):
    """
    Try to locate the 'All Expeditions' schedule table.
    Strategy:
      1) Look for heading containing 'All Expeditions' and take the first table after it.
      2) Fallback: any table whose header row contains 'Departs' and 'Returns'.
    """
    # Strategy 1
    for h in soup.find_all(re.compile(r'^h[1-4]$', re.I)):
        if 'all expeditions' in clean_text(h.get_text()).lower():
            nxt = h.find_next("table")
            if nxt:
                return nxt

    # Strategy 2 (fallback)
    for tbl in soup.find_all("table"):
        ths = [clean_text(th.get_text()) for th in tbl.find_all("th")]
        if any("departs" in t.lower() for t in ths) and any("return" in t.lower() for t in ths):
            return tbl

    return None

def parse_cabin_rows(anchor_tr):
    """
    Some sites render 'cabin breakdown' rows immediately following the main row.
    We'll scan following siblings until we hit the next 'normal' row (heuristic).
    """
    cabins = []
    tr = anchor_tr.find_next_sibling("tr")
    while tr:
        tds = tr.find_all("td")
        txt = clean_text(tr.get_text())
        # Stop if this sibling looks like a new departure (has a date)
        if looks_like_date(txt):
            break
        # Try to detect a cabin breakdown shape: (cabin type, berths left, price)
        if len(tds) >= 2:
            t0 = clean_text(tds[0].get_text())
            t1 = clean_text(tds[1].get_text())
            t2 = clean_text(tds[2].get_text()) if len(tds) >= 3 else ""

            # Heuristic: a cabin row often mentions 'cabin', 'berth', 'twin', 'premium', etc.
            if any(k in t0.lower() for k in ["cabin", "premium", "standard", "twin", "dorm", "ensuite", "king", "queen", "club"]):
                # berths
                berths = None
                m = re.search(r"(\d+)\s*(?:berths?|left|avail)", " ".join([t0, t1, t2]).lower())
                if m:
                    berths = int(m.group(1))
                price = parse_money_aud(t0) or parse_money_aud(t1) or parse_money_aud(t2)
                cabins.append({
                    "cabin_type": t0,
                    "berths_left": berths,
                    "price_aud": price
                })
        # advance
        tr = tr.find_next_sibling("tr")
    return cabins

def parse_table(tbl):
    trips = []

    # Capture header mapping
    headers = [clean_text(th.get_text()) for th in tbl.find_all("th")]
    header_idx = {h.lower(): i for i, h in enumerate(headers)}

    # Helper to get a cell by partial header name
    def get_cell(row, header_name_contains):
        for key, idx in header_idx.items():
            if header_name_contains in key:
                tds = row.find_all("td")
                if idx < len(tds):
                    return clean_text(tds[idx].get_text())
        return ""

    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        row_text = clean_text(tr.get_text()).lower()

        # Exclude sold out anywhere on the row
        if "sold out" in row_text:
            continue

        departs = get_cell(tr, "depart")
        returns = get_cell(tr, "return")
        expedition = get_cell(tr, "expedition") or get_cell(tr, "itinerary") or get_cell(tr, "trip")
        price_from_txt = get_cell(tr, "price")
        availability_txt = get_cell(tr, "availability") or get_cell(tr, "status")

        # Basic sanity: must have a departs date
        dep_date = parse_date(departs)
        ret_date = parse_date(returns)

        if not dep_date:
            # Sometimes the first td is departs if headers aren't perfect
            maybe_date = parse_date(clean_text(tds[0].get_text()))
            if maybe_date:
                dep_date = maybe_date

        # Filter by next 6 months window
        if not dep_date or not within_next_six_months(dep_date):
            continue

        # Try to infer expedition name if missing
        if not expedition:
            # look around in the row for a label that contains 'Night' (common in names)
            joined = " | ".join(clean_text(td.get_text()) for td in tds)
            m = re.search(r"(\d+\s*Night[^\|]+)", joined, flags=re.I)
            if m:
                expedition = clean_text(m.group(1))

        price_from_aud = parse_money_aud(price_from_txt)
        availability = guess_availability([availability_txt, row_text])

        # Cabin rows (best effort): scan following siblings
        cabins = parse_cabin_rows(tr)

        trips.append({
            "expedition": expedition or None,
            "departs": dep_date.isoformat() if dep_date else None,
            "returns": ret_date.isoformat() if ret_date else None,
            "price_from_aud": price_from_aud,
            "availability": availability,
            "cabins": cabins  # may be empty if no sub-rows are found
        })

    # De-duplicate identical departures (keep the one with cabins if available)
    keyed = {}
    for t in trips:
        key = (t["expedition"], t["departs"], t["returns"])
        if key not in keyed:
            keyed[key] = t
        else:
            # prefer the one that has cabins or price
            have = keyed[key]
            if (not have["cabins"] and t["cabins"]) or (not have["price_from_aud"] and t["price_from_aud"]):
                keyed[key] = t

    # Sort by departs date
    ordered = sorted(keyed.values(), key=lambda x: (x["departs"] or "9999-12-31"))
    return ordered

def main():
    html = fetch_html(SOURCE_URL)
    soup = BeautifulSoup(html, "html.parser")

    table = find_all_expeditions_table(soup)
    if not table:
        print("ERROR: Could not locate the 'All Expeditions' table on the page.", file=sys.stderr)
        trips = []
    else:
        trips = parse_table(table)

    now = datetime.now(TZ).date()
    six = (now + timedelta(days=31*6))
    payload = {
        "source_url": SOURCE_URL,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "window_start": now.isoformat(),
        "window_end": six.isoformat(),
        "trips": trips
    }

    # Write to repo root
    out_path = "mikeball_availability.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote {out_path} with {len(trips)} trips in the next 6 months.")

if __name__ == "__main__":
    main()

