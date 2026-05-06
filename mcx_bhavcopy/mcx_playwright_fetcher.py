"""
MCX Playwright Fetcher — Cloud-Ready (Linux/Windows)
=====================================================
Fetches OHLC data ONLY from MCX website bhavcopy.
Uses Playwright instead of Selenium for Linux cloud compatibility.

Auto-detects near-month expiry — no hardcoding needed.
Run schedule: Daily at 7:00 AM IST (before strategy calculations at 8:05 AM)

Install (one-time on cloud):
    pip install playwright
    playwright install chromium --with-deps
"""

import os
import re
import sys
import csv
import time
from datetime import datetime, date, timedelta
from loguru import logger

# Project root path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "mcx_ohlc")

# Instruments to fetch — (MCX symbol, CSV filename key)
COMMODITIES = [
    ("GOLD",       "gold"),
    ("GOLDM",      "goldm"),
    ("SILVER",     "silver"),
    ("SILVERM",    "silverm"),
    ("SILVERMIC",  "silvermic"),
    ("NATURALGAS", "naturalgas"),
    ("NATGASMINI", "naturalgasm"),
]

MCX_URL = "https://www.mcxindia.com/market-data/bhavcopy"


# ─── Date helpers ─────────────────────────────────────────────────────────────

def _get_last_date(csv_key: str) -> date | None:
    """Return the newest date already stored in the CSV (or None)."""
    file_path = os.path.join(DATA_DIR, f"{csv_key}_ohlc.csv")
    if not os.path.exists(file_path):
        return None
    try:
        dates = []
        with open(file_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dates.append(datetime.strptime(row["Date"], "%d %b %Y").date())
                except Exception:
                    pass
        return max(dates) if dates else None
    except Exception as e:
        logger.error(f"Error reading {csv_key}_ohlc.csv: {e}")
        return None


def _merge_and_save(csv_key: str, new_rows: list[dict]) -> int:
    """Merge new rows into existing CSV. Returns count of new rows added."""
    if not new_rows:
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, f"{csv_key}_ohlc.csv")

    existing = {}
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing[row["Date"]] = row

    added = 0
    for row in new_rows:
        if row["Date"] not in existing:
            added += 1
        existing[row["Date"]] = row  # overwrite to correct any errors

    sorted_rows = sorted(
        existing.values(),
        key=lambda x: datetime.strptime(x["Date"], "%d %b %Y"),
        reverse=True,
    )

    with open(file_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Date", "Open", "High", "Low", "Close", "Volume", "OI"]
        )
        writer.writeheader()
        writer.writerows(sorted_rows)

    logger.info(f"✅ {csv_key}_ohlc.csv — saved {len(new_rows)} rows ({added} new, {len(sorted_rows)} total)")
    return added


# ─── Playwright browser helpers ───────────────────────────────────────────────

def _setup_browser(playwright):
    """Launch headless Chromium — works on Linux cloud without display."""
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
    )
    page = context.new_page()
    return browser, context, page


def _extract_table_rows(page) -> list[dict]:
    """Extract OHLC rows from the MCX bhavcopy table."""
    rows = []
    try:
        page.wait_for_selector("#tblBhavCopy", timeout=15000)
        trs = page.query_selector_all("#tblBhavCopy tr")
        for tr in trs[1:]:  # skip header
            tds = tr.query_selector_all("td")
            if len(tds) >= 13:
                def t(i):
                    return tds[i].inner_text().strip()
                rows.append({
                    "Date":   t(0),   # e.g. "05 May 2026"
                    "Open":   t(5),
                    "High":   t(6),
                    "Low":    t(7),
                    "Close":  t(8),
                    "Volume": t(10),
                    "OI":     t(12),
                })
    except Exception as e:
        logger.warning(f"Table extraction warning: {e}")
    return rows


def _fetch_commodity(page, commodity: str, from_date_str: str, to_date_str: str) -> list[dict]:
    """
    Full 7-step MCX bhavcopy procedure for one commodity.
    from_date_str / to_date_str: "DD/MM/YYYY"
    Returns list of OHLC row dicts.
    """
    logger.info(f"Fetching {commodity} | {from_date_str} → {to_date_str}")

    # STEP 1: Open page
    page.goto(MCX_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    # STEP 2: Switch to Commodity Wise mode
    try:
        page.click("//label[contains(text(), 'Commodity Wise')]", timeout=8000)
        page.wait_for_timeout(2000)
    except Exception:
        logger.warning(f"{commodity}: Could not click Commodity Wise toggle — trying anyway")

    # STEP 3a: Select Instrument = FUTCOM
    page.select_option("#ddlInstrument", value="FUTCOM")
    page.wait_for_timeout(2000)

    # STEP 3b: Select Commodity via Telerik RadComboBox (JS injection)
    page.evaluate(f"""
        var combo = $find("ddlSymbols");
        if (combo) {{
            var item = combo.findItemByText("{commodity}");
            if (item) {{ item.select(); }}
            else {{ combo.set_text("{commodity}"); }}
        }}
    """)
    page.wait_for_timeout(2500)

    # STEP 3c: Auto-select near-month expiry (first valid option after today)
    expiry_selected = False
    today = date.today()
    try:
        options = page.query_selector_all("#ddlExpiry option")
        for opt in options:
            val = opt.get_attribute("value") or ""
            txt = opt.inner_text().strip()
            if not txt or txt.lower() in ("select", ""):
                continue
            # Parse expiry date from option text e.g. "05JUN2026"
            try:
                exp_dt = datetime.strptime(txt, "%d%b%Y").date()
                if exp_dt >= today:
                    page.select_option("#ddlExpiry", value=val)
                    logger.info(f"{commodity}: Selected expiry {txt}")
                    expiry_selected = True
                    break
            except Exception:
                continue

        if not expiry_selected and options:
            # fallback: pick first non-empty option
            for opt in options:
                val = opt.get_attribute("value") or ""
                txt = opt.inner_text().strip()
                if txt and txt.lower() not in ("select", ""):
                    page.select_option("#ddlExpiry", value=val)
                    logger.warning(f"{commodity}: Fallback expiry selected: {txt}")
                    expiry_selected = True
                    break
    except Exception as e:
        logger.error(f"{commodity}: Expiry selection failed: {e}")
        return []

    if not expiry_selected:
        logger.error(f"{commodity}: No valid expiry found")
        return []

    page.wait_for_timeout(1000)

    # STEP 3d: Set date range
    page.evaluate(f"document.getElementById('txtFromDate').value = '{from_date_str}';")
    page.evaluate(f"document.getElementById('txtToDate').value = '{to_date_str}';")

    # STEP 4: Click Show
    page.evaluate("document.getElementById('btnShowCommoditywise').click();")
    page.wait_for_timeout(5000)

    # STEP 5 & 6: Extract Page 1
    all_rows = _extract_table_rows(page)
    logger.info(f"{commodity}: Page 1 — {len(all_rows)} rows")

    # Check for Page 2
    try:
        p2 = page.query_selector("a.rgCurrentPage + a, a[title='Page 2']")
        if p2 is None:
            # Try numeric link
            p2 = page.query_selector("//a[text()='2']")
        if p2:
            p2.click()
            page.wait_for_timeout(3000)
            extra = _extract_table_rows(page)
            logger.info(f"{commodity}: Page 2 — {len(extra)} rows")
            all_rows.extend(extra)
    except Exception:
        pass  # Page 2 not available

    return all_rows


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_fetch(force_days: int = 0) -> dict:
    """
    Fetch MCX OHLC for all configured commodities.

    Args:
        force_days: If > 0, always fetch last N days regardless of CSV state.
                    If 0, fetch only missing dates (smart incremental).

    Returns:
        dict: {csv_key: "OK (N rows)" | "SKIPPED" | "FAILED (reason)"}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium --with-deps")
        return {"error": "playwright not installed"}

    today = date.today()
    to_date_str = today.strftime("%d/%m/%Y")
    summary = {}

    with sync_playwright() as pw:
        browser, context, page = _setup_browser(pw)

        try:
            for commodity, csv_key in COMMODITIES:
                try:
                    # Determine from_date
                    if force_days > 0:
                        from_date = today - timedelta(days=force_days)
                    else:
                        last_dt = _get_last_date(csv_key)
                        if last_dt and last_dt >= today - timedelta(days=1):
                            logger.info(f"{commodity}: Already up to date ({last_dt}) — skipping")
                            summary[csv_key] = "SKIPPED (up to date)"
                            continue
                        from_date = (last_dt + timedelta(days=1)) if last_dt else (today - timedelta(days=60))

                    from_date_str = from_date.strftime("%d/%m/%Y")

                    # Fetch with 1 retry on failure
                    rows = []
                    for attempt in range(2):
                        rows = _fetch_commodity(page, commodity, from_date_str, to_date_str)
                        if rows:
                            break
                        if attempt == 0:
                            logger.warning(f"{commodity}: Attempt 1 failed, retrying...")
                            page.wait_for_timeout(4000)
                            page.goto(MCX_URL, wait_until="networkidle", timeout=30000)
                            page.wait_for_timeout(3000)

                    if not rows:
                        summary[csv_key] = "FAILED (no data from MCX)"
                        logger.error(f"{commodity}: No data fetched from MCX website")
                        continue

                    added = _merge_and_save(csv_key, rows)
                    summary[csv_key] = f"OK ({len(rows)} rows, {added} new)"

                    # Small pause between commodities to avoid rate-limiting
                    page.wait_for_timeout(2000)

                except Exception as e:
                    logger.error(f"{commodity}: Unexpected error: {e}")
                    summary[csv_key] = f"FAILED ({e})"

        finally:
            context.close()
            browser.close()

    logger.info(f"MCX Playwright fetch complete: {summary}")
    return summary


if __name__ == "__main__":
    # Manual run — fetches last 60 days to backfill
    import sys
    sys.path.insert(0, PROJECT_ROOT)

    force = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    logger.info(f"Manual run — fetching last {force} days from MCX website...")
    result = run_fetch(force_days=force)
    for k, v in result.items():
        print(f"  {k:15} → {v}")
