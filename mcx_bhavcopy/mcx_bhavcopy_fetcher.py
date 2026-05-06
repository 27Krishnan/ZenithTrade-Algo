"""
MCX Bhavcopy Fetcher (Selenium-based)
======================================
Fetches correct OHLC data from MCX India Bhavcopy page using a real browser.
Replaces Angel One API which was giving incorrect Open/High/Low/Close values.

MCX site uses Akamai WAF, so direct HTTP requests are blocked (403).
We use Selenium with Chrome to navigate the page like a real user.

Commodities tracked:
    GOLD, GOLDM, SILVER, SILVERM, SILVERMIC, NATURALGAS, NATGASMINI

Usage:
    python mcx_bhavcopy_fetcher.py --inspect          # Test: fetch last 3 days of SILVERM
    python mcx_bhavcopy_fetcher.py --history          # Download Jan 1, 2026 → today
    python mcx_bhavcopy_fetcher.py --daily            # Fetch today's data (schedule at 7 AM)
    python mcx_bhavcopy_fetcher.py --commodity GOLD   # Fetch just one commodity (history)
"""

import os
import sys
import csv
import json
import time
import logging
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# Paths & Logging
# ─────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
OUTPUT_DIR  = BASE_DIR / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
LOG_FILE    = BASE_DIR / "mcx_fetcher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Commodity Configuration
# Current active expiry contracts (update when contracts roll over)
# ─────────────────────────────────────────────────────────────

COMMODITY_CONFIG = {
    "GOLD":       {"instrument": "FUTCOM", "expiry": "05JUN2026"},
    "GOLDM":      {"instrument": "FUTCOM", "expiry": "29MAY2026"},
    "SILVER":     {"instrument": "FUTCOM", "expiry": "05JUL2026"},
    "SILVERM":    {"instrument": "FUTCOM", "expiry": "30JUN2026"},
    "SILVERMIC":  {"instrument": "FUTCOM", "expiry": "30JUN2026"},
    "NATURALGAS": {"instrument": "FUTCOM", "expiry": "23JUN2026"},
    "NATGASMINI": {"instrument": "FUTCOM", "expiry": "23JUN2026"},
}

MCX_BHAVCOPY_URL = "https://www.mcxindia.com/market-data/bhavcopy"

# CSV columns
FIELDNAMES = ["Date", "Commodity", "Expiry", "Open", "High", "Low", "Close",
              "PrevClose", "Volume", "OI"]


# ─────────────────────────────────────────────────────────────
# Selenium Browser Setup
# ─────────────────────────────────────────────────────────────

def _make_driver(headless: bool = True):
    """Create a Chrome WebDriver instance."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


# ─────────────────────────────────────────────────────────────
# MCX API via XHR (intercepted through the browser session)
# ─────────────────────────────────────────────────────────────

def fetch_via_browser(commodity: str, expiry: str, from_date: str, to_date: str,
                      instrument: str = "FUTCOM", driver=None) -> list[dict]:
    """
    Use a real browser session to call the MCX API endpoint.
    The browser handles Akamai cookie/header challenges transparently.

    Args:
        from_date / to_date: "DD/MM/YYYY"
    Returns:
        List of OHLC dicts
    """
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By

    own_driver = driver is None
    if own_driver:
        driver = _make_driver(headless=True)

    try:
        # Load the page first to get Akamai cookies
        if own_driver:
            log.info(f"  Loading MCX Bhavcopy page...")
            driver.get(MCX_BHAVCOPY_URL)
            time.sleep(3)  # Let Akamai challenge resolve

        log.info(f"  API call: {commodity} ({expiry}) | {from_date} to {to_date}")
        result = driver.execute_async_script(
            """
            const [instrument, commodity, expiry, fromDate, toDate, callback] = arguments;
            fetch('https://www.mcxindia.com/backoffice/SessionValue/GetBhavCopy', {
                method: 'POST',
                headers: {
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'Content-Type': 'application/json; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Origin': 'https://www.mcxindia.com',
                    'Referer': 'https://www.mcxindia.com/market-data/bhavcopy'
                },
                body: JSON.stringify({
                    InstrumentName: instrument,
                    CommodityName: commodity,
                    ExpiryDate: expiry,
                    FromDate: fromDate,
                    ToDate: toDate,
                    OptionType: '-',
                    StrikePrice: '-'
                })
            }).then(r => r.text()).then(callback).catch(e => callback('ERROR:' + e.toString()));
            """,
            instrument, commodity, expiry, from_date, to_date
        )

        if not result or result.startswith("ERROR:"):
            log.error(f"  API error: {result}")
            return []

        log.debug(f"  Raw response (first 500): {result[:500]}")

        # Parse response
        return _parse_response(result, commodity, expiry, debug=False)

    except Exception as e:
        log.error(f"  Browser fetch error for {commodity}: {e}")
        return []
    finally:
        if own_driver and driver:
            driver.quit()


def _parse_response(raw: str, commodity: str, expiry: str, debug: bool = False) -> list[dict]:
    """Parse MCX API JSON response into clean OHLC rows."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"  JSON parse error: {e} | raw: {raw[:500]}")
        return []

    if debug:
        log.info(f"  Raw JSON keys at top level: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        if isinstance(data, dict):
            for k, v in data.items():
                sample = v[:1] if isinstance(v, list) else v
                log.info(f"    [{k}] = {json.dumps(sample, indent=2)[:300]}")

    # MCX API returns {"d": [...]} wrapper
    if isinstance(data, dict):
        raw_rows = (
            data.get("d") or
            data.get("Data") or
            data.get("data") or
            data.get("result") or
            []
        )
    elif isinstance(data, list):
        raw_rows = data
    else:
        log.warning(f"  Unexpected response type: {type(data)}")
        return []

    if not raw_rows:
        log.warning(f"  Empty data for {commodity}")
        return []

    rows = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        # Lowercase all keys for normalization
        r = {k.lower().strip(): v for k, v in row.items()}

        parsed = {
            "Date":      _parse_date(r.get("date", "") or r.get("tradingdate", "") or r.get("tradedate", "")),
            "Commodity": commodity,
            "Expiry":    expiry,
            "Open":      _to_float(r.get("open", 0) or r.get("openprice", 0)),
            "High":      _to_float(r.get("high", 0) or r.get("highprice", 0)),
            "Low":       _to_float(r.get("low", 0) or r.get("lowprice", 0)),
            "Close":     _to_float(r.get("close", 0) or r.get("closeprice", 0) or r.get("lasttradeprice", 0)),
            "PrevClose": _to_float(r.get("prevclose", 0) or r.get("pcp", 0) or r.get("previousclose", 0)),
            "Volume":    _to_int(r.get("volume", 0) or r.get("tvolume", 0) or r.get("noofcontracts", 0) or r.get("tradedvolume", 0)),
            "OI":        _to_int(r.get("oi", 0) or r.get("openinterest", 0) or r.get("openint", 0)),
        }

        if parsed["Open"] > 0 or parsed["Close"] > 0:
            rows.append(parsed)

    log.info(f"  Parsed {len(rows)} rows for {commodity}")
    return rows


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _parse_date(val) -> str:
    """Convert MCX date formats → YYYY-MM-DD."""
    if not val:
        return ""
    s = str(val).strip()

    # /Date(1746316200000)/ or /Date(1746316200000+0530)/
    if "/Date(" in s:
        try:
            ms = int(s.replace("/Date(", "").replace(")/", "").split("+")[0].split("-")[0])
            return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        except Exception:
            pass

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y",
                "%m/%d/%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return s


def _to_float(val) -> float:
    try:
        return float(str(val).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def _to_int(val) -> int:
    try:
        return int(float(str(val).replace(",", "").strip() or 0))
    except (ValueError, TypeError):
        return 0


def date_to_mcx(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def get_csv_path(commodity: str) -> Path:
    return OUTPUT_DIR / f"{commodity.lower()}_ohlc.csv"


# ─────────────────────────────────────────────────────────────
# CSV Save / Dedup
# ─────────────────────────────────────────────────────────────

def save_to_csv(rows: list[dict], commodity: str, append: bool = True):
    """Append or overwrite CSV, deduplicating by (Date, Commodity)."""
    if not rows:
        return 0

    csv_path = get_csv_path(commodity)
    existing_dates = set()

    if csv_path.exists() and append:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing_dates.add(r.get("Date", ""))

    new_rows = [r for r in rows if r["Date"] not in existing_dates]

    if not new_rows:
        log.info(f"  No new rows for {commodity} (all dates already exist)")
        return 0

    mode = "a" if (csv_path.exists() and append) else "w"
    write_header = (not csv_path.exists()) or (not append)

    with open(csv_path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    log.info(f"  Saved {len(new_rows)} new rows → {csv_path.name}")
    _sort_csv(csv_path)
    return len(new_rows)


def _sort_csv(path: Path):
    """Sort rows by Date ascending."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: r.get("Date", ""))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


# ─────────────────────────────────────────────────────────────
# High-Level Operations
# ─────────────────────────────────────────────────────────────

def download_history(from_date: date, to_date: date, commodities: list[str] = None):
    """
    Download historical OHLC data for all (or specified) commodities.
    Uses a single browser session for all requests (efficient).
    """
    if commodities is None:
        commodities = list(COMMODITY_CONFIG.keys())

    log.info("=" * 65)
    log.info(f"HISTORICAL DOWNLOAD: {from_date} → {to_date}")
    log.info(f"Commodities: {', '.join(commodities)}")
    log.info("=" * 65)

    driver = _make_driver(headless=True)
    total = 0
    try:
        log.info("Opening MCX Bhavcopy page (Akamai challenge)...")
        driver.get(MCX_BHAVCOPY_URL)
        time.sleep(4)

        for commodity in commodities:
            cfg = COMMODITY_CONFIG.get(commodity)
            if not cfg:
                log.warning(f"Unknown commodity: {commodity}")
                continue

            log.info(f"\n── {commodity} ({cfg['expiry']}) ──")
            rows = fetch_via_browser(
                commodity=commodity,
                expiry=cfg["expiry"],
                from_date=date_to_mcx(from_date),
                to_date=date_to_mcx(to_date),
                instrument=cfg["instrument"],
                driver=driver,
            )
            n = save_to_csv(rows, commodity, append=False)
            total += n
            time.sleep(1.5)

    finally:
        driver.quit()

    log.info(f"\n✅ History download complete. Total rows: {total}")
    _print_summary(commodities)


def fetch_daily(target_date: date = None, commodities: list[str] = None):
    """
    Fetch OHLC for one day (default: today). Call at 7:00 AM daily.
    Appends to existing CSVs.
    """
    if target_date is None:
        target_date = date.today()
    if commodities is None:
        commodities = list(COMMODITY_CONFIG.keys())

    log.info("=" * 65)
    log.info(f"DAILY FETCH: {target_date}")
    log.info("=" * 65)

    driver = _make_driver(headless=True)
    total = 0
    try:
        driver.get(MCX_BHAVCOPY_URL)
        time.sleep(4)

        date_str = date_to_mcx(target_date)
        for commodity in commodities:
            cfg = COMMODITY_CONFIG.get(commodity)
            if not cfg:
                continue

            log.info(f"\n── {commodity} ──")
            rows = fetch_via_browser(
                commodity=commodity,
                expiry=cfg["expiry"],
                from_date=date_str,
                to_date=date_str,
                instrument=cfg["instrument"],
                driver=driver,
            )
            n = save_to_csv(rows, commodity, append=True)
            total += n
            time.sleep(0.8)

    finally:
        driver.quit()

    log.info(f"\n✅ Daily fetch complete. Rows saved: {total}")
    _print_summary(commodities)


def inspect(commodity: str = "SILVERM"):
    """Debug: show raw API data for last 7 days with full field names."""
    cfg = COMMODITY_CONFIG.get(commodity, {"instrument": "FUTCOM", "expiry": "30JUN2026"})
    to_dt = date.today()
    from_dt = to_dt - timedelta(days=7)

    driver = _make_driver(headless=False)  # visible for debug
    raw_result = None
    try:
        driver.get(MCX_BHAVCOPY_URL)
        time.sleep(4)

        log.info(f"  API call: {commodity} ({cfg['expiry']}) | {date_to_mcx(from_dt)} to {date_to_mcx(to_dt)}")
        raw_result = driver.execute_async_script(
            """
            const [instrument, commodity, expiry, fromDate, toDate, callback] = arguments;
            fetch('https://www.mcxindia.com/backoffice/SessionValue/GetBhavCopy', {
                method: 'POST',
                headers: {
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'Content-Type': 'application/json; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Origin': 'https://www.mcxindia.com',
                    'Referer': 'https://www.mcxindia.com/market-data/bhavcopy'
                },
                body: JSON.stringify({
                    InstrumentName: instrument,
                    CommodityName: commodity,
                    ExpiryDate: expiry,
                    FromDate: fromDate,
                    ToDate: toDate,
                    OptionType: '-',
                    StrikePrice: '-'
                })
            }).then(r => r.text()).then(callback).catch(e => callback('ERROR:' + e.toString()));
            """,
            cfg["instrument"], commodity, cfg["expiry"],
            date_to_mcx(from_dt), date_to_mcx(to_dt)
        )
    finally:
        driver.quit()

    if not raw_result or raw_result.startswith("ERROR:"):
        log.error(f"API call failed: {raw_result}")
        return []

    log.info(f"\n=== RAW RESPONSE (first 1000 chars) ===")
    log.info(raw_result[:1000])

    rows = _parse_response(raw_result, commodity, cfg["expiry"], debug=True)

    if rows:
        log.info(f"\nDate         Open        High         Low       Close")
        log.info("-" * 60)
        for r in rows:
            log.info(f"{r['Date']:<12} {r['Open']:>10.2f} {r['High']:>10.2f} {r['Low']:>10.2f} {r['Close']:>10.2f}")
    else:
        log.warning("No data returned. Check expiry date in COMMODITY_CONFIG.")

    return rows


def _print_summary(commodities: list[str] = None):
    if commodities is None:
        commodities = list(COMMODITY_CONFIG.keys())
    log.info("\n📊 Data Summary:")
    log.info(f"{'Commodity':<15} {'Rows':>6} {'From':<12} {'To':<12}")
    log.info("-" * 50)
    for c in commodities:
        path = get_csv_path(c)
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows:
                dates = sorted(r["Date"] for r in rows if r["Date"])
                log.info(f"{c:<15} {len(rows):>6} {dates[0]:<12} {dates[-1]:<12}")
            else:
                log.info(f"{c:<15} {'0':>6}")
        else:
            log.info(f"{c:<15} {'N/A':>6}")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MCX Bhavcopy OHLC Fetcher")
    ap.add_argument("--history",    action="store_true", help="Download Jan 1 2026 → today")
    ap.add_argument("--daily",      action="store_true", help="Fetch today's data")
    ap.add_argument("--inspect",    action="store_true", help="Debug: show last 7 days of SILVERM")
    ap.add_argument("--commodity",  metavar="NAME",      help="Specific commodity (e.g. GOLD)")
    ap.add_argument("--from-date",  metavar="DD/MM/YYYY",help="Custom start date")
    ap.add_argument("--to-date",    metavar="DD/MM/YYYY",help="Custom end date (default: today)")
    args = ap.parse_args()

    commodities = [args.commodity.upper()] if args.commodity else None

    if args.inspect:
        c = (args.commodity or "SILVERM").upper()
        inspect(c)

    elif args.history or args.from_date:
        from_dt = (
            datetime.strptime(args.from_date, "%d/%m/%Y").date()
            if args.from_date else date(2026, 1, 1)
        )
        to_dt = (
            datetime.strptime(args.to_date, "%d/%m/%Y").date()
            if args.to_date else date.today()
        )
        download_history(from_dt, to_dt, commodities)

    elif args.daily:
        fetch_daily(commodities=commodities)

    else:
        # Default: inspect mode
        inspect(args.commodity or "SILVERM")
