#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Local (headful) scraper for Mike Ball availability.

Why headful? The site uses WP admin-ajax + runtime tokens/grecaptcha.
A visible browser run on your PC gets the right cookies & tokens,
so the search returns full HTML and we can parse it reliably.

Usage (local):
  python scripts/scrape.py --window --out mikeball_availability.json --headful
  # or explicit:
  python scripts/scrape.py --start 2025-10-01 --end 2026-03-31 --out mikeball_availability.json --headful
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict

from playwright.sync_api import sync_playwright, Response, BrowserContext, Page

SOURCE_URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"

# -------------------- text/date helpers --------------------

def parse_money_to_int(s: Optional[str]) -> Optional[int]:
    if not s: return None
    v = re.sub(r"[^\d.]", "", s)
    if not v: return None
    try: return int(round(float(v)))
    except: return None

def to_date_obj(txt: str) -> Optional[date]:
    if not txt: return None
    clean = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\\s+", "", txt.strip(), flags=re.I)
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            pass
    return None

def norm_availability(s: str) -> str:
    t = (s or "").strip().lower()
    if "sold" in t: return "Sold Out"
    if "hurry" in t or "few" in t: return "Few left"
    if "10+" in t or "avail" in t: return "Available"
    return s.strip() if s else "—"

def extract_int_in_text(s: str) -> Optional[int]:
    if not s: return None
    m = re.search(r"\\d+", s)
    return int(m.group()) if m else None

def within_window(d: Optional[date], start: Optional[date], end: Optional[date]) -> bool:
    if not d: return False
    if start and d < start: return False
    if end and d > end: return False
    return True

def fmt_picker(d: date) -> str:
    # e.g. "Friday 12 September 2025"
    return d.strftime("%A %d %B %Y")

# -------------------- page helpers --------------------

def _fire_input_change(page: Page, start_txt: str, end_txt: str):
    page.evaluate(
        """
        (args) => {
          const { s, e } = args;
          function fire(el){
            ['input','change','keyup','blur'].forEach(ev => el.dispatchEvent(new Event(ev, {bubbles:true})));
          }
          const a = document.querySelector('#starts_at');
          const b = document.querySelector('#ends_at');
          if (a) { a.value = s; fire(a); }
          if (b) { b.value = e; fire(b); }
        }
        """,
        {"s": start_txt, "e": end_txt}
    )

def _close_datepickers(page: Page):
    try:
        # Press OK on any open pickers
        ok_btns = page.locator(".dtp .dtp-btn-ok")
        for i in range(ok_btns.count()):
            try: ok_btns.nth(i).click(timeout=400)
            except: pass
        # ESC twice
        page.keyboard.press("Escape")
        page.wait_for_timeout(120)
        page.keyboard.press("Escape")
        # Last resort: hide overlays
        page.evaluate("document.querySelectorAll('.dtp').forEach(el => el.style.display = 'none')")
    except:
        pass

def _ensure_consent(page: Page):
    # Click any cookie banners if visible
    try:
        for sel in ["button:has-text('Accept')", "[aria-label*='accept']"]:
            btns = page.locator(sel)
            if btns.count():
                try: btns.first.click(timeout=800)
                except: pass
    except: pass

def _submit_search_belt_and_braces(page: Page):
    # Dispatch submit on form AND click the visible button; some themes listen to one or the other.
    try:
        page.evaluate("""() => {
            const f = document.querySelector('.resco-form');
            if (f) f.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
        }""")
    except: pass
    try:
        page.click("button.ra-ajax", timeout=15000)
    except: pass
    # Also click the primary button if it differs
    try:
        page.click(".resco-form .btn.btn-primary:not(.ra-ajax)", timeout=3000)
    except: pass

def _wait_results_dom(page: Page, timeout_ms: int = 70000) -> None:
    # Wait until container exists
    page.wait_for_selector("#availability-results", state="attached", timeout=15000)
    # Poll for rows or substantial innerHTML
    page.wait_for_function(
        """() => {
            const tgt = document.querySelector('#availability-results');
            if (!tgt) return false;
            if (tgt.querySelector('table tbody tr')) return true;
            return (tgt.innerHTML || '').length > 2000; // coarse fallback
        }""",
        timeout=timeout_ms
    )

def perform_search(page: Page, ctx: BrowserContext, start_d: date, end_d: date):
    page.goto(SOURCE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    _ensure_consent(page)

    # Fill dates (type + programmatic events)
    start_txt = fmt_picker(start_d)
    end_txt   = fmt_picker(end_d)
    page.fill("#starts_at", "")
    page.type("#starts_at", start_txt, delay=3)
    page.fill("#ends_at", "")
    page.type("#ends_at", end_txt, delay=3)
    _fire_input_change(page, start_txt, end_txt)

    # Expedition = ALL
    try: page.select_option("select[name='name']", value="all")
    except: pass

    # Uncheck "Hide unavailable"
    try:
        box = page.locator("input[name='hide_unavailable']")
        if box.is_checked(): box.uncheck()
    except: pass

    _close_datepickers(page)
    _submit_search_belt_and_braces(page)
    _wait_results_dom(page, timeout_ms=80000)

    # Expand all “See more”
    for sel in ("#availability-results >> text=See more", "#availability-results :text('See more')"):
        btns = page.locator(sel)
        for i in range(btns.count()):
            try: btns.nth(i).click(timeout=800)
            except: pass
    page.wait_for_timeout(400)

def extract_from_results(page: Page, start_date: Optional[date], end_date: Optional[date]) -> List[Dict]:
    trips: List[Dict] = []
    rows = page.locator("#availability-results table tbody tr")
    rcount = rows.count()

    i = 0
    while i < rcount:
        tr = rows.nth(i)
        cells = tr.locator("td")
        if cells.count() >= 5:
            title = cells.nth(0).inner_text().strip()
            dep   = cells.nth(1).inner_text().strip()
            ret   = cells.nth(2).inner_text().strip()
            price = cells.nth(3).inner_text().strip()
            av    = cells.nth(4).inner_text().strip()

            d = to_date_obj(dep)
            if within_window(d, start_date, end_date):
                cabins: List[Dict] = []
                cabins_left = None
                # If next row holds cabin table
                if i + 1 < rcount:
                    nxt = rows.nth(i+1)
                    text = nxt.inner_text().lower()
                    if "cabin type" in text or "berths left" in text:
                        tbl = nxt.locator("table:has-text('Cabin Type')")
                        if tbl.count():
                            body_rows = tbl.nth(0).locator("tbody tr")
                            for ri in range(body_rows.count()):
                                tds = body_rows.nth(ri).locator("td").all_inner_texts()
                                if len(tds) >= 3:
                                    cabins.append({
                                        "type": tds[0].strip(),
                                        "available": extract_int_in_text(tds[1]),
                                        "priceAUD": parse_money_to_int(tds[2]),
                                    })
                            cabins_left = sum((c["available"] or 0) for c in cabins) if cabins else None
                        i += 1
                trips.append({
                    "title": title,
                    "dateText": dep,
                    "dateReturn": ret,
                    "priceFromAUD": parse_money_to_int(price),
                    "availability": norm_availability(av),
                    "cabinsLeft": cabins_left,
                    "link": SOURCE_URL,
                    "cabins": cabins
                })
        i += 1

    trips.sort(key=lambda t: to_date_obj(t.get("dateText","")) or date(1900,1,1))
    return trips

# -------------------- runner --------------------

def run_scrape(start_date: date, end_date: date, headful: bool=False) -> Dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Brisbane",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 1600}
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        try:
            perform_search(page, ctx, start_date, end_date)
            trips = extract_from_results(page, start_date, end_date)
        except Exception as e:
            # Save debug artifacts so we can adjust selectors if needed
            try:
                page.screenshot(path="debug_screen.png", full_page=True)
                with open("debug_page.html","w",encoding="utf-8") as f:
                    f.write(page.content())
                print("❌ Error; saved debug_screen.png and debug_page.html")
            except: pass
            raise e
        finally:
            ctx.close(); browser.close()

    return {
        "scrapedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": SOURCE_URL,
        "trips": trips
    }

# -------------------- CLI --------------------

def parse_args():
    ap = argparse.ArgumentParser(description="Local headful scraper for Mike Ball availability.")
    ap.add_argument("--start", help="Start date YYYY-MM-DD")
    ap.add_argument("--end", help="End date YYYY-MM-DD")
    ap.add_argument("--window", action="store_true",
                    help="Rolling 4→26 weeks from today (ignored if explicit dates provided)")
    ap.add_argument("--out", default="mikeball_availability.json", help="Output JSON path")
    ap.add_argument("--headful", action="store_true", help="Show browser window (recommended locally)")
    return ap.parse_args()

def main():
    args = parse_args()
    if not args.window and not (args.start and args.end):
        args.window = True

    if args.window:
        today = date.today()
        start_d = today + timedelta(days=28)   # 4 weeks
        end_d   = today + timedelta(days=182)  # 26 weeks
    else:
        try:
            start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
            end_d   = datetime.strptime(args.end, "%Y-%m-%d").date()
        except ValueError:
            print("Dates must be YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)

    data = run_scrape(start_d, end_d, headful=args.headful)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ Wrote {len(data['trips'])} trips to {args.out}")

if __name__ == "__main__":
    main()
