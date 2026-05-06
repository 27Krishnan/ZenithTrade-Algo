"""
MCX Actions Fetcher — Runs inside GitHub Actions (Ubuntu runner)
=================================================================
Uses Selenium + Chrome (available on GitHub Actions runners).
Fetches OHLC data from MCX website and saves to data/mcx_ohlc/*.csv

This script is called ONLY by the GitHub Actions workflow.
It runs on GitHub's servers — not on GCP — so MCX doesn't block it.
"""

import os
import re
import sys
import csv
import time
from datetime import datetime, date, timedelta
from loguru import logger

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "mcx_ohlc")

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


# ─── CSV helpers ──────────────────────────────────────────────────────────────

def _get_last_date(csv_key: str) -> date | None:
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
    except Exception:
        return None


def _merge_and_save(csv_key: str, new_rows: list[dict]) -> int:
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
        existing[row["Date"]] = row
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
    logger.info(f"✅ {csv_key}_ohlc.csv — {len(new_rows)} rows ({added} new, {len(sorted_rows)} total)")
    return added


# ─── Selenium driver ──────────────────────────────────────────────────────────

def _setup_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # Use system Chrome if available (GitHub Actions has google-chrome-stable)
    chrome_path = "/usr/bin/google-chrome-stable"
    if os.path.exists(chrome_path):
        opts.binary_location = chrome_path

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


# ─── Fetch logic ──────────────────────────────────────────────────────────────

def _extract_table(driver) -> list[dict]:
    from selenium.webdriver.common.by import By
    rows = []
    try:
        table = driver.find_element(By.ID, "tblBhavCopy")
        trs = table.find_elements(By.TAG_NAME, "tr")
        for tr in trs[1:]:
            tds = tr.find_elements(By.TAG_NAME, "td")
            if len(tds) >= 13:
                def t(i): return tds[i].text.strip()
                rows.append({
                    "Date":   t(0),
                    "Open":   t(5),
                    "High":   t(6),
                    "Low":    t(7),
                    "Close":  t(8),
                    "Volume": t(10),
                    "OI":     t(12),
                })
    except Exception as e:
        logger.warning(f"Table extract warning: {e}")
    return rows


def _fetch_commodity(driver, commodity: str, from_date_str: str, to_date_str: str) -> list[dict]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC

    today = date.today()

    logger.info(f"{commodity}: Opening MCX bhavcopy page...")
    driver.get(MCX_URL)
    time.sleep(5)

    # Switch to Commodity Wise
    try:
        toggle = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//label[contains(text(), 'Commodity Wise')]")
            )
        )
        toggle.click()
        time.sleep(2)
    except Exception:
        logger.warning(f"{commodity}: Could not click Commodity Wise toggle")

    # Select Instrument
    Select(driver.find_element(By.ID, "ddlInstrument")).select_by_value("FUTCOM")
    time.sleep(2)

    # Select Commodity via JS
    driver.execute_script(f"""
        var combo = $find("ddlSymbols");
        if (combo) {{
            var item = combo.findItemByText("{commodity}");
            if (item) {{ item.select(); }}
            else {{ combo.set_text("{commodity}"); }}
        }}
    """)
    time.sleep(2)

    # Auto-select near-month expiry
    expiry_selected = False
    for _ in range(5):
        try:
            sel = Select(driver.find_element(By.ID, "ddlExpiry"))
            for opt in sel.options:
                txt = opt.text.strip()
                val = opt.get_attribute("value")
                if not txt or txt.lower() in ("select", ""):
                    continue
                try:
                    exp_dt = datetime.strptime(txt, "%d%b%Y").date()
                    if exp_dt >= today:
                        sel.select_by_value(val)
                        logger.info(f"{commodity}: Selected expiry {txt}")
                        expiry_selected = True
                        break
                except Exception:
                    continue
            if expiry_selected:
                break
        except Exception:
            time.sleep(2)

    if not expiry_selected:
        logger.error(f"{commodity}: No valid expiry found")
        return []

    # Set date range
    driver.execute_script(f"document.getElementById('txtFromDate').value = '{from_date_str}';")
    driver.execute_script(f"document.getElementById('txtToDate').value = '{to_date_str}';")

    # Click Show
    driver.execute_script("document.getElementById('btnShowCommoditywise').click();")
    time.sleep(5)

    rows = _extract_table(driver)
    logger.info(f"{commodity}: Page 1 → {len(rows)} rows")

    # Page 2
    try:
        p2 = driver.find_element(By.LINK_TEXT, "2")
        p2.click()
        time.sleep(3)
        extra = _extract_table(driver)
        logger.info(f"{commodity}: Page 2 → {len(extra)} rows")
        rows.extend(extra)
    except Exception:
        pass

    return rows


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_fetch(force_days: int = 5) -> dict:
    today = date.today()
    to_date_str = today.strftime("%d/%m/%Y")
    summary = {}

    driver = _setup_driver()
    try:
        for commodity, csv_key in COMMODITIES:
            try:
                if force_days > 0:
                    from_date = today - timedelta(days=force_days)
                else:
                    last_dt = _get_last_date(csv_key)
                    if last_dt and last_dt >= today - timedelta(days=1):
                        logger.info(f"{commodity}: Already up to date — skipping")
                        summary[csv_key] = "SKIPPED"
                        continue
                    from_date = (last_dt + timedelta(days=1)) if last_dt else (today - timedelta(days=60))

                rows = []
                for attempt in range(2):
                    rows = _fetch_commodity(driver, commodity, from_date.strftime("%d/%m/%Y"), to_date_str)
                    if rows:
                        break
                    if attempt == 0:
                        logger.warning(f"{commodity}: Empty result, retrying...")
                        time.sleep(4)
                        driver.get(MCX_URL)
                        time.sleep(4)

                if not rows:
                    summary[csv_key] = "FAILED (no data)"
                    continue

                added = _merge_and_save(csv_key, rows)
                summary[csv_key] = f"OK ({len(rows)} rows, {added} new)"
                time.sleep(2)

            except Exception as e:
                logger.error(f"{commodity}: {e}")
                summary[csv_key] = f"FAILED ({e})"

    finally:
        driver.quit()

    logger.info(f"Done: {summary}")
    return summary


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    logger.info(f"GitHub Actions MCX fetch — last {days} days")
    result = run_fetch(force_days=days)
    for k, v in result.items():
        print(f"  {k:15} → {v}")
    # Exit with error if all failed
    if all("FAILED" in v for v in result.values()):
        sys.exit(1)
