#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mike Ball availability scraper (RescoApi form-aware).

Flow:
- Open page with Playwright (headless Chromium)
- Fill Start Date = today (Sydney), End Date = +6 months (long, human format)
- Select Expedition = "all" (– ALL EXPEDITIONS –)
- Tick "Hide unavailable expeditions"
- Click the Search button (class .ra-ajax) so the plugin renders results into #availability-results
- Wait for #availability-results table to appear
- Parse rows: Expedition, Departs, Returns, Price From (AUD), Availability
- Try to parse the following "Cabin Type" breakdown table per departure (if present)
- Exclude SOLD OUT rows
- Write mikeball_availability.json
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

def within_next_six_months(d):
    if not d: return False
    now = datetime.now(TZ).date()
    six = now + timedelta(days=31*6)
    return now <= d <= six

def long_date(d):  # page expects long text (e.g., "Friday 12 September 2025")
    return d.strftime("%A %d %B %Y")

def get_results_html():
    today = datetime.now(TZ).date()
    end   = today + timedelta(days=31*6)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1300, "height": 2200})
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800)

        # Fill dates by direct input (ids come from the HTML you pasted)
        try:
            page.fill("#starts_at", long_date(today))
            page.fill("#ends_at",   long_date(end))
        except Exception:
            pass  # if ids ever change, the form may already default a range

        # Select "-- ALL EXPEDITIONS --" (value="all")
        try:
            page.select_option("select[name='name']", value="all")
        except Exception:
            pass

        # Tick "Hide unavailable expeditions"
        try:
            page.check("input[name='hide_unavailable']")
        except Exception:
            pass

        # Click Search (button with class ra-ajax)
        try:
            page.click("button.ra-ajax", timeout=5000)
        except Exception:
            # fallback: any button named Search
            try:
                page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=3000)
            except Exception:
                pass

        # Wait for the plugin to render the results into #availability-results
        # First the container becomes non-empty, then a table appears.
        try:
            page.wait_for_selector("#availability-results", timeout=20000)
            # ensure table with Departs/Returns headers exists
            page.wait_for_selector(
                "#availability-results table >> xpath=.//th[contains(.,'Departs')]",
                timeout=20000
            )
        except PWTimeout:
            # small grace period; some runs are just a bit slow
            page.wait_for_timeout(1500)

        # Expand "See more" links so cabin tables render (if present)
        try:
            for btn in page.locator("#availability-results >> text=/^\\s*See\\s*more\\s*$/i").all():
                try: btn.click(timeout=800)
                except Exception: pass
            page.wait_for_timeout(400)
        except Exception:
            pass

        html = page.content()
        browser.close()
        return html, today, end

def parse_results(html: str):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#availability-results")
    if not root:
        return []

    # Find the first results table that has Departs and Returns
    table = None
    for tbl in root.find_all("table"):
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
        # Expected columns: [Expeditions..., Departs, Returns, Price From (AUD), Availability, (Enquire btn)]
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

        # Look for an immediate following row with a nested table having "Cabin Type"
        cabins = []
        if i + 1 < len(rows):
            sib = rows[i+1]
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
    seen = set()
    uniq = []
    for t in trips:
        key = (t["expedition"], t["departs"], t["returns"])
        if key not in seen:
            seen.add(key); uniq.append(t)
    uniq.sort(key=lambda x: x["departs"] or "9999-12-31")
    return uniq

def main():
    html, start_date, end_date = get_results_html()
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
