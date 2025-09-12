#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Headless scrape of:
https://www.mikeball.com/availability-mike-ball-dive-expeditions/

What it does:
- Sets Start Date = today (Sydney), End Date = +6 months
- Ensures "All Expeditions" is selected
- Checks "Hide unavailable expeditions"
- Clicks Search and waits for the results table
- Parses each available departure row (Expedition, Departs, Returns, Price From (AUD), Availability)
- Best-effort parse of the expanded Cabin table (Cabin Type, Berths Left, Price)
- Writes mikeball_availability.json
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
    m = re.search(r"([0-9][0-9,]*(?:\.\d{2})?)", text.replace(",", ""))
    return int(round(float(m.group(1)))) if m else None

def parse_date(text: str):
    if not text: return None
    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None

def within_window(d, start, end):
    return bool(d and start <= d <= end)

def set_date_input(page, label_regex, value_str):
    # Find the input by its accessible name (label text next to it) and fill
    inp = page.get_by_label(label_regex, exact=False)
    try:
        inp.clear()
    except Exception:
        pass
    inp.fill(value_str)

def get_rendered_html():
    today = datetime.now(TZ).date()
    end   = today + timedelta(days=31*6)  # ~6 months
    # The site uses long-form dates (e.g., "Friday 12 September 2025") in the inputs.
    def long_date(d):
        return d.strftime("%A %d %B %Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 2000})
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        # Give client-side JS a moment
        page.wait_for_timeout(1200)

        # 1) Set date range
        try:
            set_date_input(page, re.compile(r"start date", re.I), long_date(today))
            set_date_input(page, re.compile(r"end date", re.I),   long_date(end))
        except Exception:
            pass  # if inputs differ, the default window may already be OK

        # 2) Ensure "All Expeditions" is selected (do nothing if site default is already that)
        try:
            sel = page.get_by_label(re.compile(r"expedition", re.I))
            # If there is a dropdown, choose first/ALL option
            try:
                sel.select_option(index=0)
            except Exception:
                pass
        except Exception:
            pass

        # 3) Tick "Hide unavailable expeditions"
        try:
            cb = page.get_by_role("checkbox", name=re.compile(r"hide.*unavailable", re.I))
            cb.check()
        except Exception:
            pass

        # 4) Click Search
        try:
            page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=2000)
        except Exception:
            pass

        # 5) Wait for a table that has headers Departs & Returns
        try:
            page.wait_for_selector(
                "xpath=//table[.//th[contains(.,'Departs')] and .//th[contains(.,'Returns')]]",
                timeout=15000
            )
        except PWTimeout:
            # one more small wait; sometimes it renders just after
            page.wait_for_timeout(1500)

        # Expand “See more” (to expose cabin table under rows), if present
        try:
            for btn in page.locator("text=/^\\s*See\\s*more\\s*$/i").all():
                try:
                    btn.click(timeout=800)
                except Exception:
                    pass
            page.wait_for_timeout(600)
        except Exception:
            pass

        html = page.content()
        browser.close()
        return html, today, end

def parse_page(html, start_date, end_date):
    soup = BeautifulSoup(html, "html.parser")

    # Find the main result table by headers (robust to caption text)
    table = None
    for tbl in soup.find_all("table"):
        heads = [clean(th.get_text()) for th in tbl.find_all("th")]
        hj = " | ".join(heads).lower()
        if ("depart" in hj) and ("return" in hj) and ("price" in hj) and ("avail" in hj):
            table = tbl
            break
    if not table:
        return []

    trips = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        row_text = clean(tr.get_text()).lower()
        if "sold out" in row_text:
            continue

        cols = [clean(td.get_text()) for td in tds]
        if len(cols) < 4:
            continue

        expedition = cols[0]
        departs_txt = cols[1] if len(cols) >= 2 else ""
        returns_txt = cols[2] if len(cols) >= 3 else ""
        price_txt   = cols[3] if len(cols) >= 4 else ""
        avail_txt   = cols[4] if len(cols) >= 5 else ""

        dep = parse_date(departs_txt)
        ret = parse_date(returns_txt)
        if not within_window(dep, start_date, end_date):
            continue

        # Try to find a following row with a "Cabin Type" sub-table
        cabins = []
        sib = tr.find_next_sibling("tr")
        if sib and sib.find("th", string=re.compile(r"Cabin Type", re.I)):
            for row in sib.find_all("tr"):
                ctd = [clean(x.get_text()) for x in row.find_all("td")]
                if not ctd:
                    continue
                ctype = ctd[0]
                berths = None
                price_aud = None
                for cell in ctd[1:]:
                    m = re.search(r"(\d+)\s*(?:berths?|left|avail)", cell.lower())
                    if m:
                        berths = int(m.group(1))
                    p = parse_money_aud(cell)
                    if p:
                        price_aud = p
                if ctype:
                    cabins.append({
                        "cabin_type": ctype,
                        "berths_left": berths,
                        "price_aud": price_aud
                    })

        trips.append({
            "expedition": expedition or None,
            "departs": dep.isoformat() if dep else None,
            "returns": ret.isoformat() if ret else None,
            "price_from_aud": parse_money_aud(price_txt),
            "availability": (avail_txt or None),
            "cabins": cabins
        })

    trips.sort(key=lambda x: x["departs"] or "9999-12-31")
    return trips

def main():
    html, start_date, end_date = get_rendered_html()
    trips = parse_page(html, start_date, end_date)
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
