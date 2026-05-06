"""
MCX HTTP Fetcher — No Browser Required!
=========================================
Fetches OHLC directly from MCX website using pure HTTP requests.
No Selenium, No Playwright, No Chrome needed.
Works on any Linux/Windows server.

How it works:
  1. GET the MCX bhavcopy page → extract ASP.NET VIEWSTATE tokens
  2. POST the form with commodity filters + date range
  3. Parse the HTML table → save to CSV

Run schedule: Daily at 7:00 AM IST (before strategy calculations at 8:05 AM)
"""

import os
import re
import csv
import time
from datetime import datetime, date, timedelta
from loguru import logger

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    raise ImportError("Run: pip install requests beautifulsoup4")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "mcx_ohlc")

MCX_URL = "https://www.mcxindia.com/market-data/bhavcopy"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": MCX_URL,
}

# Instruments to fetch — (MCX symbol name, CSV filename key)
COMMODITIES = [
    ("GOLD",       "gold"),
    ("GOLDM",      "goldm"),
    ("SILVER",     "silver"),
    ("SILVERM",    "silverm"),
    ("SILVERMIC",  "silvermic"),
    ("NATURALGAS", "naturalgas"),
    ("NATGASMINI", "naturalgasm"),
]


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
    except Exception as e:
        logger.error(f"Error reading {csv_key}_ohlc.csv: {e}")
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
    logger.info(f"✅ {csv_key}_ohlc.csv — {len(new_rows)} rows saved ({added} new, {len(sorted_rows)} total)")
    return added


# ─── MCX HTTP helpers ─────────────────────────────────────────────────────────

def _get_page_tokens(session: requests.Session) -> dict:
    """GET the MCX bhavcopy page and extract ASP.NET form tokens."""
    resp = session.get(MCX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    tokens = {}
    for field in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
        el = soup.find("input", {"name": field})
        if el:
            tokens[field] = el.get("value", "")

    return tokens


def _get_expiry_options(session: requests.Session, tokens: dict, commodity: str) -> list[str]:
    """
    POST to switch to Commodity Wise mode and select the commodity.
    Returns list of available expiry option values.
    """
    # Switch to Commodity Wise tab via __doPostBack
    data = {
        **tokens,
        "__EVENTTARGET":   "ctl00$ContentPlaceHolder1$LiveData1$rdbBhavCopyMode",
        "__EVENTARGUMENT": "",
        "ctl00$ContentPlaceHolder1$LiveData1$rdbBhavCopyMode": "rbCommodityWise",
        "ctl00$ContentPlaceHolder1$LiveData1$ddlInstrument":   "FUTCOM",
    }
    resp = session.post(MCX_URL, data=data, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Re-read tokens from updated page
    for field in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
        el = soup.find("input", {"name": field})
        if el:
            tokens[field] = el.get("value", "")

    # Trigger commodity selection to populate expiry dropdown
    data2 = {
        **tokens,
        "__EVENTTARGET":   "ctl00$ContentPlaceHolder1$LiveData1$rcbCommodityName",
        "__EVENTARGUMENT": "",
        "ctl00$ContentPlaceHolder1$LiveData1$rdbBhavCopyMode":  "rbCommodityWise",
        "ctl00$ContentPlaceHolder1$LiveData1$ddlInstrument":    "FUTCOM",
        "ctl00$ContentPlaceHolder1$LiveData1$rcbCommodityName": commodity,
    }
    resp2 = session.post(MCX_URL, data=data2, headers=HEADERS, timeout=30)
    resp2.raise_for_status()
    soup2 = BeautifulSoup(resp2.text, "html.parser")

    # Re-read tokens again
    for field in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
        el = soup2.find("input", {"name": field})
        if el:
            tokens[field] = el.get("value", "")

    # Extract expiry options
    expiry_select = soup2.find("select", {"id": re.compile("ddlExpiry", re.I)})
    options = []
    if expiry_select:
        for opt in expiry_select.find_all("option"):
            val = opt.get("value", "").strip()
            txt = opt.get_text(strip=True)
            if val and txt.lower() not in ("select", ""):
                options.append((val, txt))
    return options, tokens


def _fetch_table(
    session: requests.Session,
    tokens: dict,
    commodity: str,
    expiry_val: str,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """Submit the form and parse the results table."""
    data = {
        **tokens,
        "__EVENTTARGET":   "",
        "__EVENTARGUMENT": "",
        "ctl00$ContentPlaceHolder1$LiveData1$rdbBhavCopyMode":  "rbCommodityWise",
        "ctl00$ContentPlaceHolder1$LiveData1$ddlInstrument":    "FUTCOM",
        "ctl00$ContentPlaceHolder1$LiveData1$rcbCommodityName": commodity,
        "ctl00$ContentPlaceHolder1$LiveData1$ddlExpiry":        expiry_val,
        "ctl00$ContentPlaceHolder1$LiveData1$txtFromDate":      from_date,
        "ctl00$ContentPlaceHolder1$LiveData1$txtToDate":        to_date,
        "ctl00$ContentPlaceHolder1$LiveData1$btnShowCommoditywise": "Show",
    }
    resp = session.post(MCX_URL, data=data, headers=HEADERS, timeout=45)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    table = soup.find("table", {"id": re.compile("tblBhavCopy", re.I)})
    if not table:
        # Try any results table
        table = soup.find("table", {"class": re.compile("tbl|grid|bhav", re.I)})
    if not table:
        logger.warning(f"{commodity}: No results table found in response")
        return []

    trs = table.find_all("tr")
    for tr in trs[1:]:  # skip header
        tds = tr.find_all("td")
        if len(tds) >= 13:
            def t(i):
                return tds[i].get_text(strip=True)
            rows.append({
                "Date":   t(0),
                "Open":   t(5),
                "High":   t(6),
                "Low":    t(7),
                "Close":  t(8),
                "Volume": t(10),
                "OI":     t(12),
            })

    return rows


def _fetch_commodity(commodity: str, csv_key: str, from_date_str: str, to_date_str: str) -> list[dict]:
    """Full pipeline for one commodity."""
    today = date.today()
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        logger.info(f"{commodity}: Getting page tokens...")
        tokens = _get_page_tokens(session)

        logger.info(f"{commodity}: Getting expiry options...")
        expiry_options, tokens = _get_expiry_options(session, tokens, commodity)

        if not expiry_options:
            logger.error(f"{commodity}: No expiry options found")
            return []

        # Pick nearest future expiry
        chosen_val = None
        for val, txt in expiry_options:
            try:
                exp_dt = datetime.strptime(txt, "%d%b%Y").date()
                if exp_dt >= today:
                    chosen_val = val
                    logger.info(f"{commodity}: Using expiry {txt}")
                    break
            except Exception:
                continue

        if not chosen_val:
            chosen_val = expiry_options[0][0]
            logger.warning(f"{commodity}: Using fallback expiry {expiry_options[0][1]}")

        logger.info(f"{commodity}: Fetching data {from_date_str} → {to_date_str}...")
        rows = _fetch_table(session, tokens, commodity, chosen_val, from_date_str, to_date_str)
        logger.info(f"{commodity}: Got {len(rows)} rows from MCX")
        return rows

    except Exception as e:
        logger.error(f"{commodity}: HTTP fetch failed: {e}")
        return []
    finally:
        session.close()


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_fetch(force_days: int = 0) -> dict:
    """
    Fetch MCX OHLC for all commodities from MCX website (pure HTTP, no browser).

    Args:
        force_days: If > 0, fetch last N days. If 0, smart incremental.
    Returns:
        summary dict
    """
    # Ensure beautifulsoup4 is available
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        logger.error("beautifulsoup4 not installed. Run: pip install beautifulsoup4")
        return {"error": "beautifulsoup4 not installed"}

    today = date.today()
    to_date_str = today.strftime("%d/%m/%Y")
    summary = {}

    for commodity, csv_key in COMMODITIES:
        try:
            if force_days > 0:
                from_date = today - timedelta(days=force_days)
            else:
                last_dt = _get_last_date(csv_key)
                if last_dt and last_dt >= today - timedelta(days=1):
                    logger.info(f"{commodity}: Already up to date ({last_dt}) — skipping")
                    summary[csv_key] = "SKIPPED"
                    continue
                from_date = (last_dt + timedelta(days=1)) if last_dt else (today - timedelta(days=60))

            from_date_str = from_date.strftime("%d/%m/%Y")

            rows = []
            for attempt in range(2):
                rows = _fetch_commodity(commodity, csv_key, from_date_str, to_date_str)
                if rows:
                    break
                if attempt == 0:
                    logger.warning(f"{commodity}: Attempt 1 empty, retrying in 5s...")
                    time.sleep(5)

            if not rows:
                summary[csv_key] = "FAILED (no data from MCX)"
                continue

            added = _merge_and_save(csv_key, rows)
            summary[csv_key] = f"OK ({len(rows)} rows, {added} new)"

            time.sleep(3)  # be polite to MCX servers

        except Exception as e:
            logger.error(f"{commodity}: {e}")
            summary[csv_key] = f"FAILED ({e})"

    logger.info(f"MCX HTTP fetch complete: {summary}")
    return summary


if __name__ == "__main__":
    import sys
    sys.path.insert(0, PROJECT_ROOT)
    force = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    logger.info(f"Manual run — fetching last {force} days from MCX website (HTTP mode)...")
    result = run_fetch(force_days=force)
    for k, v in result.items():
        print(f"  {k:15} → {v}")
