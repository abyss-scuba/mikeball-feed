#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mike Ball availability scraper.

This version bypasses the visible date-picker and calls the same WordPress
admin-ajax endpoint used by Mike Ball's RESCO availability widget.

It was written because the visible fields can show correct dates while the
front-end widget still rejects them via its internal datepicker state.

Usage:
  python scripts/scrape.py --window --out mikeball_availability.json --headful
  python scripts/scrape.py --start 2026-08-02 --end 2027-01-03 --out mikeball_availability.json --headful
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import BrowserContext, Page, sync_playwright

SOURCE_URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
DEFAULT_AJAX_URL = "https://www.mikeball.com/wp-admin/admin-ajax.php"


# -------------------- text/date helpers --------------------

def parse_money_to_int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    v = re.sub(r"[^\d.]", "", str(s))
    if not v:
        return None
    try:
        return int(round(float(v)))
    except Exception:
        return None


def extract_int_in_text(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\d+", str(s))
    if not m:
        return None
    try:
        return int(m.group())
    except Exception:
        return None


def to_date_obj(txt: str) -> Optional[date]:
    if not txt:
        return None

    clean = re.sub(
        r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+",
        "",
        txt.strip(),
        flags=re.I,
    )

    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            pass

    return None


def fmt_picker(d: date) -> str:
    # The RESCO script sends this exact human-readable format to admin-ajax.
    return d.strftime("%A %d %B %Y")


def within_window(d: Optional[date], start: Optional[date], end: Optional[date]) -> bool:
    if not d:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def norm_availability(s: Optional[str]) -> str:
    t = (s or "").strip().lower()
    if not t:
        return "—"
    if "sold" in t:
        return "Sold Out"
    if "hurry" in t or "few" in t:
        return "Few left"
    if "10+" in t or "avail" in t:
        return "Available"
    return (s or "").strip()


# -------------------- HTTP / AJAX helpers --------------------

def _ensure_consent(page: Page) -> None:
    for sel in [
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "[aria-label*='accept' i]",
        ".cky-btn-accept",
        ".cc-allow",
    ]:
        try:
            btns = page.locator(sel)
            if btns.count():
                btns.first.click(timeout=800)
                page.wait_for_timeout(250)
        except Exception:
            pass


def _get_ajax_url(page: Page) -> str:
    try:
        ajax_url = page.evaluate(
            "() => window.rescoAjax && window.rescoAjax.ajaxurl ? window.rescoAjax.ajaxurl : null"
        )
        if ajax_url:
            return str(ajax_url)
    except Exception:
        pass
    return DEFAULT_AJAX_URL


def _post_ajax(ctx: BrowserContext, ajax_url: str, form: Dict[str, str]) -> Dict[str, Any]:
    response = ctx.request.post(
        ajax_url,
        form=form,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SOURCE_URL,
            "Origin": "https://www.mikeball.com",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=60000,
    )

    text = response.text()

    if not response.ok:
        raise RuntimeError(
            f"AJAX request failed with HTTP {response.status}. "
            f"Response preview: {text[:1000]}"
        )

    try:
        return response.json()
    except Exception:
        try:
            return json.loads(text)
        except Exception as exc:
            raise RuntimeError(
                "AJAX response was not JSON. "
                f"Response preview: {text[:1500]}"
            ) from exc


def search_availability(
    ctx: BrowserContext,
    ajax_url: str,
    start_d: date,
    end_d: date,
    expedition: str = "all",
) -> str:
    start_txt = fmt_picker(start_d)
    end_txt = fmt_picker(end_d)

    payload = {
        "action": "ra_search_availability",
        "data[name]": expedition,
        "data[starts_at]": start_txt,
        "data[ends_at]": end_txt,
    }

    data = _post_ajax(ctx, ajax_url, payload)

    if data.get("success") and data.get("html"):
        return str(data["html"])

    if data.get("errors"):
        raise RuntimeError(f"Mike Ball returned validation errors: {data['errors']}")

    raise RuntimeError(f"Unexpected Mike Ball search response: {json.dumps(data)[:2000]}")


def expand_berths(ctx: BrowserContext, ajax_url: str, depart_id: str) -> Dict[str, Any]:
    if not depart_id:
        return {}

    payload = {
        "action": "ra_expand_berths",
        "data[id]": str(depart_id),
    }

    try:
        return _post_ajax(ctx, ajax_url, payload)
    except Exception as exc:
        # A single cabin-detail failure should not kill the whole feed.
        print(f"Warning: could not expand berths for departure {depart_id}: {exc}", file=sys.stderr)
        return {}


# -------------------- HTML parsing via Playwright DOM --------------------

def _extract_summary_rows(page: Page, html: str) -> List[Dict[str, Any]]:
    page.set_content(
        "<!doctype html><html><body><div id='availability-results'>"
        + html
        + "</div></body></html>",
        wait_until="domcontentloaded",
    )

    return page.evaluate(
        """
        () => {
          const rows = [];
          document.querySelectorAll('#availability-results table tbody tr').forEach((tr) => {
            if (tr.classList.contains('berth-row')) return;

            const cells = Array.from(tr.children)
              .filter(el => el.tagName === 'TD')
              .map(td => (td.innerText || '').replace(/\\s+/g, ' ').trim());

            if (cells.length < 3) return;

            rows.push({
              id: tr.getAttribute('data-id') || tr.dataset.id || '',
              dataDate: tr.getAttribute('data-date') || tr.dataset.date || '',
              className: tr.className || '',
              cells: cells,
              availabilityText: tr.querySelector('.depart-avail')
                ? tr.querySelector('.depart-avail').innerText.replace(/\\s+/g, ' ').trim()
                : ''
            });
          });
          return rows;
        }
        """
    )


def _find_trip_fields(cells: List[str]) -> Tuple[str, str, str, str, str]:
    """
    Mike Ball's table may include a leading expand/control cell.
    This finds the date cells first, then infers title/price/availability.
    """
    date_indices = []
    for i, text in enumerate(cells):
        if to_date_obj(text):
            date_indices.append(i)

    if len(date_indices) >= 2:
        dep_i, ret_i = date_indices[0], date_indices[1]
        title = cells[dep_i - 1] if dep_i > 0 else cells[0]
        dep = cells[dep_i]
        ret = cells[ret_i]

        price = ""
        availability = ""

        for text in cells[ret_i + 1:]:
            if not price and "$" in text:
                price = text
                continue
            if text and text != price:
                availability = text

        if not availability and cells:
            availability = cells[-1]

        return title, dep, ret, price, availability

    # Fallback to the old scraper's original column assumptions.
    if len(cells) >= 5:
        return cells[0], cells[1], cells[2], cells[3], cells[4]

    padded = cells + [""] * 5
    return padded[0], padded[1], padded[2], padded[3], padded[4]


def _parse_cabins_from_html(page: Page, berths_html: str) -> List[Dict[str, Any]]:
    if not berths_html:
        return []

    rows = page.evaluate(
        """
        (html) => {
          const div = document.createElement('div');
          div.innerHTML = html;
          return Array.from(div.querySelectorAll('tr')).map(tr =>
            Array.from(tr.children)
              .filter(el => ['TD', 'TH'].includes(el.tagName))
              .map(td => (td.innerText || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean)
          ).filter(row => row.length);
        }
        """,
        berths_html,
    )

    cabins: List[Dict[str, Any]] = []
    for row in rows:
        if not row:
            continue

        joined = " ".join(row).lower()
        if "cabin type" in joined or "berth" in row[0].lower() and "left" in joined:
            continue

        if len(row) >= 3:
            cabins.append(
                {
                    "type": row[0],
                    "available": extract_int_in_text(row[1]),
                    "priceAUD": parse_money_to_int(row[2]),
                }
            )

    return cabins


def extract_trips(
    page: Page,
    ctx: BrowserContext,
    ajax_url: str,
    html: str,
    start_date: Optional[date],
    end_date: Optional[date],
) -> List[Dict[str, Any]]:
    summary_rows = _extract_summary_rows(page, html)
    trips: List[Dict[str, Any]] = []

    for row in summary_rows:
        cells = row.get("cells", [])
        title, dep, ret, price, availability = _find_trip_fields(cells)
        dep_date = to_date_obj(dep)

        if not within_window(dep_date, start_date, end_date):
            continue

        depart_id = str(row.get("id") or "")
        cabins: List[Dict[str, Any]] = []
        cabins_left: Optional[int] = None

        if depart_id:
            berth_response = expand_berths(ctx, ajax_url, depart_id)
            cabins = _parse_cabins_from_html(page, str(berth_response.get("berths", "")))

            if cabins:
                cabins_left = sum((c.get("available") or 0) for c in cabins)

            if berth_response.get("avail") not in (None, ""):
                availability = str(berth_response.get("avail"))

        trips.append(
            {
                "title": title,
                "dateText": dep,
                "dateReturn": ret,
                "priceFromAUD": parse_money_to_int(price),
                "availability": norm_availability(availability or row.get("availabilityText")),
                "cabinsLeft": cabins_left,
                "link": SOURCE_URL,
                "cabins": cabins,
                "sourceId": depart_id,
            }
        )

    trips.sort(key=lambda t: to_date_obj(t.get("dateText", "")) or date(1900, 1, 1))
    return trips


# -------------------- runner --------------------

def run_scrape(start_date: date, end_date: date, headful: bool = False) -> Dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headful,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx = browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Brisbane",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1600},
        )

        page = ctx.new_page()
        page.set_default_timeout(30000)

        try:
            page.goto(SOURCE_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            _ensure_consent(page)

            ajax_url = _get_ajax_url(page)
            html = search_availability(ctx, ajax_url, start_date, end_date, "all")
            trips = extract_trips(page, ctx, ajax_url, html, start_date, end_date)

            # Helpful debug files even on success.
            page.set_content(
                "<!doctype html><html><body><div id='availability-results'>"
                + html
                + "</div></body></html>",
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            try:
                page.screenshot(path="debug_screen.png", full_page=True)
                with open("debug_page.html", "w", encoding="utf-8") as f:
                    try:
                        f.write(page.content())
                    except Exception:
                        f.write("")
                    f.write("\n\n<!-- scraper_error:\n")
                    f.write(str(exc))
                    f.write("\n-->\n")
                print("❌ Error; saved debug_screen.png and debug_page.html")
            except Exception:
                pass
            raise
        finally:
            ctx.close()
            browser.close()

    return {
        "scrapedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": SOURCE_URL,
        "trips": trips,
    }


# -------------------- CLI --------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Scrape Mike Ball availability using RESCO admin-ajax.")
    ap.add_argument("--start", help="Start date YYYY-MM-DD")
    ap.add_argument("--end", help="End date YYYY-MM-DD")
    ap.add_argument(
        "--window",
        action="store_true",
        help="Rolling 4→26 weeks from today. Used if explicit dates are not supplied.",
    )
    ap.add_argument("--out", default="mikeball_availability.json", help="Output JSON path")
    ap.add_argument("--headful", action="store_true", help="Show browser window")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    if args.start and args.end:
        try:
            start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
            end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
        except ValueError:
            print("Dates must be YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        today = date.today()
        start_d = today + timedelta(days=28)
        end_d = today + timedelta(days=182)

    data = run_scrape(start_d, end_d, headful=args.headful)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ Wrote {len(data['trips'])} trips to {args.out}")


if __name__ == "__main__":
    main()
