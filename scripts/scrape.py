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
    return int(round(float(m.group(1)))) if m else None

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
    return d.strftime("%A %d %B %Y")  # e.g. Friday 12 September 2025

def get_results_html():
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

        # Fill the form and fire input/change events
        js_fill = """
        (data) => {
          const fire = (el) => {
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          };
          const s = document.querySelector('#starts_at');
          const e = document.querySelector('#ends_at');
          if (s) { s.value = data.dStart; fire(s); }
          if (e) { e.value = data.dEnd;   fire(e); }
          const sel = document.querySelector("select[name='name']");
          if (sel) { sel.value = 'all'; fire(sel); }
          const cb  = document.querySelector("input[name='hide_unavailable']");
          if (cb && !cb.checked) { cb.click(); }
        }
        """
        page.evaluate(js_fill, {"dStart": long_date(today), "dEnd": long_date(end)})

        # Click the AJAX Search button
        try:
            page.click("button.ra-ajax", timeout=5000)
        except Exception:
            try:
                page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=3000)
            except Exception:
                pass

        # Wait for results in #availability-results
        try:
            page.wait_for_selector("#availability-results", timeout=25000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_selector("#availability-results table tbody tr", timeout=15000)
        except PWTimeout:
            page.wait_for_timeout(2000)

        # Expand “See more” in results for cabin tables
        try:
            for btn in page.locator("#availability-results >> text=/^\\s*See\\s*more\\s*$/i").all():
                try: btn.click(timeout=800)
                except Exception: pass
            page.wait_for_timeout(400)
        except Exception:
            pass

        # Only grab the results container HTML (easier to parse)
        try:
            results_html = page.locator("#availability-results").first.evaluate("el => el.innerHTML")
        except Exception:
            results_html = ""

        # Optional debug snapshot
        with open("mikeball_results_debug.html", "w", encoding="utf-8") as f:
            f.write(results_html or "")

        browser.close()
        return results_html, today, end

def parse_results(results_html: str):
    if not results_html:
        return []
    soup = BeautifulSoup(results_html, "html.parser")

    # Find the results table that has departs/returns
    table = None
    for tbl in soup.find_all("table"):
        heads = [clean(th.get_text()) for th in tbl.find_all("th")]
        hdr = " | ".join(heads).lower()
        if "depart" in hdr and "return" in hdr:
            table = tbl; break
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

        # Cabin table is usually the next <tr> with a nested table
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
    seen, out = set(), []
    for t in trips:
        key = (t["expedition"], t["departs"], t["returns"])
        if key not in seen:
            seen.add(key); out.append(t)
    out.sort(key=lambda x: x["departs"] or "9999-12-31")
    return out

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
