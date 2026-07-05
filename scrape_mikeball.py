#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Mike Ball Dive Expeditions availability (with cabins) directly from the
public page by submitting the visible search form. No guessing AJAX/nonce.

USAGE EXAMPLES
--------------
# Rolling window: show only departures 4–26 weeks from today (recommended)
python scrape_mikeball.py --window --out mikeball_availability.json

# Fixed date range
python scrape_mikeball.py --start 2025-10-01 --end 2026-03-31 --out mikeball_availability.json

# Debug with a visible browser window
python scrape_mikeball.py --window --headful
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SOURCE_URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"

# ====================== Basic helpers ======================

def parse_money_to_int(s: Optional[str]) -> Optional[int]:
    """
    Convert '$4,802' to 4802 (AUD integer). Returns None if not parseable.
    """
    if not s:
        return None
    v = re.sub(r"[^\d.]", "", s)
    if not v:
        return None
    try:
        return int(round(float(v)))
    except Exception:
        return None

def to_date_obj(txt: str) -> Optional[date]:
    """
    Accepts 'Thu 11 Sep 2025' or '11 Sep 2025' or '11 September 2025'.
    Returns a date or None.
    """
    if not txt:
        return None
    clean = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+", "", txt.strip(), flags=re.I)
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            pass
    return None

def norm_availability(s: str) -> str:
    """
    Normalize availability text to consistent labels for your page.
    """
    t = (s or "").strip().lower()
    if "sold" in t:
        return "Sold Out"
    if "hurry" in t or "few" in t:
        return "Few left"
    if "10+" in t or "avail" in t:
        return "Available"
    return s.strip() if s else "—"

def extract_int_in_text(s: str) -> Optional[int]:
    """
    First integer found in text: '10+ - See more' -> 10
    """
    if not s:
        return None
    m = re.search(r"\d+", s)
    if m:
        try:
            return int(m.group())
        except Exception:
            return None
    return None

def within_window(d: Optional[date], start: Optional[date], end: Optional[date]) -> bool:
    if not d:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True

# ====================== Page/form helpers ======================

def fmt_picker(d: date) -> str:
    """
    Resco’s datepicker accepts human-readable text; this format works:
    "Friday 12 September 2025"
    """
    return d.strftime("%A %d %B %Y")

def perform_search(page: Page, ctx: BrowserContext, start_d: date, end_d: date):
    page.goto(SOURCE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    _ensure_consent(page)

    page.evaluate(
        """({sy, sm, sd, ey, em, ed}) => {
            if (window.moment) {
                window.moment.locale("en");
            }

            function fire(el) {
                ["input", "change", "keyup", "blur"].forEach(ev => {
                    el.dispatchEvent(new Event(ev, { bubbles: true, cancelable: true }));
                });
            }

            function setPicker(selector, y, m, d) {
                const el = document.querySelector(selector);
                if (!el) throw new Error(selector + " not found");

                const mo = window.moment ? window.moment([y, m, d]).locale("en") : null;
                const text = mo ? mo.format("dddd DD MMMM YYYY") : "";

                if (window.jQuery) {
                    const $el = window.jQuery(el);

                    try {
                        if ($el.bootstrapMaterialDatePicker && mo) {
                            $el.bootstrapMaterialDatePicker("setDate", mo);
                        }
                    } catch (e) {}

                    $el.val(text)
                        .trigger("input")
                        .trigger("change")
                        .trigger("keyup")
                        .trigger("blur");
                }

                el.value = text;
                el.setAttribute("value", text);
                fire(el);
            }

            document.querySelectorAll(".dtp").forEach(el => {
                el.style.display = "none";
                el.classList.add("hidden");
            });

            setPicker("#starts_at", sy, sm, sd);
            setPicker("#ends_at", ey, em, ed);

            const select = document.querySelector("select[name='name']");
            if (select) {
                select.value = "all";
                select.dispatchEvent(new Event("change", { bubbles: true }));
                if (window.jQuery) {
                    window.jQuery(select).val("all").trigger("change");
                }
            }

            const hiddenName = document.querySelector("input[type='hidden'][name='name']");
            if (hiddenName) {
                hiddenName.value = "all";
                hiddenName.setAttribute("value", "all");
            }

            const hide = document.querySelector("input[name='hide_unavailable']");
            if (hide) {
                hide.checked = false;
                hide.dispatchEvent(new Event("change", { bubbles: true }));
            }
        }""",
        {
            "sy": start_d.year,
            "sm": start_d.month - 1,
            "sd": start_d.day,
            "ey": end_d.year,
            "em": end_d.month - 1,
            "ed": end_d.day,
        }
    )

    page.wait_for_timeout(1000)

    values = page.evaluate(
        """() => ({
            start: document.querySelector("#starts_at")?.value || "",
            end: document.querySelector("#ends_at")?.value || "",
            body: document.body.innerText || ""
        })"""
    )

    print("Date fields before search:", values["start"], "to", values["end"])

    page.click("button.ra-ajax")
    page.wait_for_timeout(2500)

    after = page.evaluate(
        """() => ({
            start: document.querySelector("#starts_at")?.value || "",
            end: document.querySelector("#ends_at")?.value || "",
            body: document.body.innerText || ""
        })"""
    )

    print("Date fields after search:", after["start"], "to", after["end"])

    if "Enter a valid start date" in after["body"] or "Enter a valid end date" in after["body"]:
        raise RuntimeError(
            "Mike Ball form rejected the date fields. "
            f"Before search: {values['start']} to {values['end']}. "
            f"After search: {after['start']} to {after['end']}."
        )

    _wait_results_dom(page, timeout_ms=80000)

    for sel in ("#availability-results >> text=See more", "#availability-results :text('See more')"):
        btns = page.locator(sel)
        for i in range(btns.count()):
            try:
                btns.nth(i).click(timeout=800)
            except Exception:
                pass

    page.wait_for_timeout(400)
def extract_from_results(page, start_date: Optional[date], end_date: Optional[date]) -> List[Dict]:
    """
    Parse the summary rows and the immediate details (cabins) row if present.
    """
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

                # If next row is a detail row with a cabin table, parse it
                if i + 1 < rcount:
                    nxt = rows.nth(i+1)
                    if "cabin type" in nxt.inner_text().lower():
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
                        i += 1  # Skip detail row in main loop

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

    # Sort by departure date
    trips.sort(key=lambda t: to_date_obj(t.get("dateText","")) or date(1900,1,1))
    return trips

# ====================== Scraper runner ======================

def run_scrape(start_date: Optional[date], end_date: Optional[date], headful: bool=False) -> Dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1600})
        page = ctx.new_page()
        page.set_default_timeout(20000)

        try:
            perform_search(page, start_date, end_date)
            trips = extract_from_results(page, start_date, end_date)
        except Exception as e:
            # Debug artifacts: screenshot + full HTML
            try:
                page.screenshot(path="debug_screen.png")
                with open("debug_page.html","w",encoding="utf-8") as f:
                    f.write(page.content())
                print("❌ Error while scraping. Saved debug_screen.png and debug_page.html")
            except Exception:
                pass
            raise e
        finally:
            ctx.close()
            browser.close()

    return {
        "scrapedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": SOURCE_URL,
        "trips": trips
    }

# ====================== CLI ======================

def parse_args():
    ap = argparse.ArgumentParser(description="Scrape Mike Ball availability with cabin details.")
    ap.add_argument("--start", help="Start date (YYYY-MM-DD). Optional if --window.")
    ap.add_argument("--end", help="End date (YYYY-MM-DD). Optional if --window.")
    ap.add_argument("--window", action="store_true",
                    help="Rolling 4→26 weeks from today (ignores --start/--end).")
    ap.add_argument("--out", default="mikeball_availability.json", help="Output JSON path.")
    ap.add_argument("--headful", action="store_true", help="Show browser window.")
    return ap.parse_args()

def main():
    args = parse_args()

    if args.window:
        today = date.today()
        start_d = today + timedelta(days=28)   # 4 weeks ahead
        end_d   = today + timedelta(days=182)  # 26 weeks ahead
    else:
        if not args.start or not args.end:
            print("Please provide --start and --end or use --window", file=sys.stderr)
            sys.exit(1)
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
