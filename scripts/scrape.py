TEMPORARY DEBUG scraper for Mike Ball availability.

Purpose:
  This version is designed to discover how Mike Ball's RESCO availability
  widget submits its AJAX request. It loads the page, captures the RESCO
  JavaScript details, prints them to the GitHub Actions log, saves debug files,
  and then stops on purpose.

Use:
  python scripts/scrape.py --window --out mikeball_availability.json --headful
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any

from playwright.sync_api import sync_playwright, BrowserContext, Page

SOURCE_URL = "https://www.mikeball.com/availability-mike-ball-dive-expeditions/"
LAST_DEBUG: Dict[str, Any] = {}


# ====================== Basic helpers ======================

def parse_money_to_int(s: Optional[str]) -> Optional[int]:
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
    t = (s or "").strip().lower()
    if "sold" in t:
        return "Sold Out"
    if "hurry" in t or "few" in t:
        return "Few left"
    if "10+" in t or "avail" in t:
        return "Available"
    return s.strip() if s else "—"


def extract_int_in_text(s: str) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        return int(m.group())
    except Exception:
        return None


def within_window(d: Optional[date], start: Optional[date], end: Optional[date]) -> bool:
    if not d:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def fmt_picker(d: date) -> str:
    return d.strftime("%A %d %B %Y")


# ====================== Page/debug helpers ======================

def _ensure_consent(page: Page) -> None:
    """Click common cookie/consent buttons if they appear."""
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
                page.wait_for_timeout(300)
        except Exception:
            pass


def _safe_text_response(ctx: BrowserContext, url: str, limit: int = 60000) -> Dict[str, Any]:
    """Fetch a text asset using Playwright's request context, bypassing browser CORS."""
    try:
        response = ctx.request.get(url, timeout=30000)
        text = response.text() if response.ok else ""
        return {
            "url": url,
            "status": response.status,
            "ok": response.ok,
            "contentType": response.headers.get("content-type", ""),
            "textPreview": text[:limit],
            "textLength": len(text),
        }
    except Exception as exc:
        return {
            "url": url,
            "error": repr(exc),
        }


def _capture_resco_debug(page: Page, ctx: BrowserContext) -> Dict[str, Any]:
    """Capture enough information to determine the RESCO AJAX action and payload."""
    debug = page.evaluate(
        """
        () => {
          function safeString(fn, limit) {
            try { return String(fn).slice(0, limit || 12000); }
            catch (e) { return 'stringify failed: ' + e.message; }
          }

          const resco = window.RESCO || null;
          const rescoKeys = resco ? Object.getOwnPropertyNames(resco) : [];
          const rescoFunctions = {};

          if (resco) {
            rescoKeys.forEach(k => {
              try {
                if (typeof resco[k] === 'function') {
                  rescoFunctions[k] = safeString(resco[k], 20000);
                } else {
                  rescoFunctions[k] = {
                    type: typeof resco[k],
                    value: JSON.stringify(resco[k]).slice(0, 4000)
                  };
                }
              } catch (e) {
                rescoFunctions[k] = 'capture failed: ' + e.message;
              }
            });
          }

          const allScripts = Array.from(document.scripts).map(s => ({
            id: s.id || '',
            src: s.src || '',
            textPreview: s.src ? '' : (s.textContent || '').slice(0, 5000)
          }));

          const rescoScriptUrls = allScripts
            .map(s => s.src)
            .filter(src => /RescoApi|resco/i.test(src));

          const form = document.querySelector('.resco-form');
          const formInputs = form ? Array.from(form.querySelectorAll('input, select, button')).map(el => ({
            tag: el.tagName,
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            id: el.id || '',
            className: el.className || '',
            value: el.value || '',
            checked: !!el.checked,
            text: (el.innerText || el.textContent || '').trim().slice(0, 300)
          })) : [];

          return {
            url: location.href,
            title: document.title,
            hasJQuery: !!window.jQuery,
            hasMoment: !!window.moment,
            momentLocale: window.moment ? window.moment.locale() : null,
            hasRESCO: !!window.RESCO,
            rescoKeys,
            rescoFunctions,
            rescoAjax: window.rescoAjax || null,
            wpAjaxUrl: window.ajaxurl || null,
            grecaptchaPresent: !!window.grecaptcha,
            formHtml: form ? form.outerHTML.slice(0, 12000) : 'resco form not found',
            formInputs,
            rescoScriptUrls,
            allScripts,
            availabilityResultsHtml: document.querySelector('#availability-results')
              ? document.querySelector('#availability-results').outerHTML.slice(0, 12000)
              : 'availability-results not found',
            bodyTextPreview: (document.body.innerText || '').slice(0, 12000)
          };
        }
        """
    )

    # Also fetch the RESCO script source, which is the most useful part.
    fetched_scripts = []
    for url in debug.get("rescoScriptUrls", []):
        fetched_scripts.append(_safe_text_response(ctx, url, limit=80000))
    debug["fetchedRescoScripts"] = fetched_scripts

    return debug


def perform_search(page: Page, ctx: BrowserContext, start_d: date, end_d: date) -> None:
    """
    TEMPORARY DEBUG VERSION.

    This loads the Mike Ball page, captures the RESCO widget JavaScript details,
    prints them, and then stops intentionally. The workflow is expected to fail
    while we inspect the printed RESCO DEBUG block.
    """
    global LAST_DEBUG

    page.goto(SOURCE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    _ensure_consent(page)

    try:
        page.wait_for_function("() => !!window.RESCO && !!window.RESCO.initAvailabilitySearch", timeout=30000)
    except Exception:
        # Continue anyway; the debug output will show whether RESCO loaded.
        pass

    LAST_DEBUG = _capture_resco_debug(page, ctx)

    print("RESCO DEBUG START")
    print(json.dumps(LAST_DEBUG, indent=2, ensure_ascii=False))
    print("RESCO DEBUG END")

    raise RuntimeError("Stopped after RESCO debug capture. This failure is intentional.")


# ====================== Existing parser retained for later ======================

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
            dep = cells.nth(1).inner_text().strip()
            ret = cells.nth(2).inner_text().strip()
            price = cells.nth(3).inner_text().strip()
            av = cells.nth(4).inner_text().strip()

            d = to_date_obj(dep)
            if within_window(d, start_date, end_date):
                cabins: List[Dict] = []
                cabins_left = None

                if i + 1 < rcount:
                    nxt = rows.nth(i + 1)
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
                    "cabins": cabins,
                })
        i += 1

    trips.sort(key=lambda t: to_date_obj(t.get("dateText", "")) or date(1900, 1, 1))
    return trips


# ====================== Scraper runner ======================

def run_scrape(start_date: date, end_date: date, headful: bool = False) -> Dict:
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
            perform_search(page, ctx, start_date, end_date)
            trips = extract_from_results(page, start_date, end_date)
        except Exception as exc:
            try:
                page.screenshot(path="debug_screen.png", full_page=True)

                page_html = page.content()
                debug_comment = "\n\n<!-- RESCO_DEBUG_JSON_START\n" + json.dumps(
                    LAST_DEBUG,
                    indent=2,
                    ensure_ascii=False,
                ) + "\nRESCO_DEBUG_JSON_END -->\n"

                with open("debug_page.html", "w", encoding="utf-8") as f:
                    f.write(page_html)
                    f.write(debug_comment)

                print("❌ Error; saved debug_screen.png and debug_page.html")
            except Exception as save_exc:
                print(f"Could not save debug files: {save_exc}", file=sys.stderr)
            raise exc
        finally:
            ctx.close()
            browser.close()

    return {
        "scrapedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": SOURCE_URL,
        "trips": trips,
    }


# ====================== CLI ======================

def parse_args():
    ap = argparse.ArgumentParser(description="Debug Mike Ball RESCO availability widget.")
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
        # Default to rolling window, even if --window was omitted.
        today = date.today()
        start_d = today + timedelta(days=28)
        end_d = today + timedelta(days=182)

    data = run_scrape(start_d, end_d, headful=args.headful)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ Wrote {len(data['trips'])} trips to {args.out}")


if __name__ == "__main__":
    main()
