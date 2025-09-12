def perform_search(page, start_d: date, end_d: date):
    """Open page, fill dates, set expedition=ALL, close pickers, click Search, expand 'See more'."""
    page.goto(SOURCE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    # Type dates into inputs
    page.fill("#starts_at", "")
    page.type("#starts_at", fmt_picker(start_d), delay=5)
    page.fill("#ends_at", "")
    page.type("#ends_at", fmt_picker(end_d), delay=5)

    # Expedition = ALL (value='all')
    try:
        page.select_option("select[name='name']", value="all")
    except:
        pass

    # Uncheck "Hide unavailable" (if checked)
    try:
        box = page.locator("input[name='hide_unavailable']")
        if box.is_checked():
            box.uncheck()
    except:
        pass

    # --- NEW: ensure any material datepicker overlay is closed ---
    try:
        # Preferred: click the OK buttons on any visible pickers
        ok_btns = page.locator(".dtp .dtp-btn-ok")
        for i in range(ok_btns.count()):
            try:
                ok_btns.nth(i).click(timeout=500)
            except:
                pass

        # Extra safety: press Escape a couple of times
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")

        # Nuclear option: hide any remaining overlays so they can't intercept clicks
        page.evaluate("document.querySelectorAll('.dtp').forEach(el => el.style.display = 'none')")
    except:
        pass
    # -------------------------------------------------------------

    # Make sure the Search button is interactable
    page.wait_for_selector("button.ra-ajax", state="visible", timeout=10000)
    page.wait_for_function(
        """() => {
            const b = document.querySelector('button.ra-ajax');
            if (!b) return false;
            const s = getComputedStyle(b);
            return s.display !== 'none' && s.visibility !== 'hidden';
        }""",
        timeout=10000
    )

    # Click Search (.ra-ajax)
    page.click("button.ra-ajax", timeout=20000)

    # Wait for results and at least one row
    page.wait_for_selector("#availability-results", timeout=30000)
    page.wait_for_selector("#availability-results table tbody tr", timeout=30000)

    # Expand all “See more” to reveal cabins
    see_more = page.locator("#availability-results :text('See more')")
    for i in range(see_more.count()):
        try:
            see_more.nth(i).click(timeout=1000)
        except:
            pass
    page.wait_for_timeout(800)
