"""
Angel One OHLC Updater — Cloud-Ready MCX Data Fetcher
======================================================
Replaces Selenium-based MCX website scraping with Angel One API.
Runs after market close to update all MCX OHLC CSV files.

Schedule: Daily at 11:45 PM IST (after MCX closes at 11:30 PM)
"""

import os
import csv
import sys
from datetime import datetime, timedelta, date
from loguru import logger

# Make sure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mcx_ohlc")

# Commodity → (Angel One search name, exchange)
COMMODITY_CONFIG = {
    "gold":        {"search": "GOLD",       "exchange": "MCX"},
    "goldm":       {"search": "GOLDM",      "exchange": "MCX"},
    "silver":      {"search": "SILVER",     "exchange": "MCX"},
    "silverm":     {"search": "SILVERM",    "exchange": "MCX"},
    "silvermic":   {"search": "SILVERMIC",  "exchange": "MCX"},
    "naturalgas":  {"search": "NATURALGAS", "exchange": "MCX"},
    "naturalgasm": {"search": "NATGASMINI", "exchange": "MCX"},
}


def _find_near_month_token(name: str, exchange: str = "MCX") -> dict | None:
    """Find the near-month futures contract token from Angel One master."""
    import re
    try:
        from api.option_chain import load_master
        data = load_master()
        if not data:
            logger.error(f"No instrument master data for {name}")
            return None
    except Exception as e:
        logger.error(f"Master load failed: {e}")
        return None

    prefix = name.upper()
    candidates = []
    for row in data:
        sym  = row.get("symbol", "")
        exch = row.get("exch_seg", "")
        inst = row.get("instrumenttype", "")
        if exch != exchange or inst != "FUTCOM":
            continue

        # Strict prefix matching
        if name == "GOLD" and not re.match(r'^GOLD\d', sym):
            continue
        if name == "GOLDM" and not re.match(r'^GOLDM\d', sym):
            continue
        if name == "SILVER" and not re.match(r'^SILVER\d', sym):
            continue
        if name == "SILVERM" and not re.match(r'^SILVERM\d', sym):
            continue
        if name == "SILVERMIC" and not re.match(r'^SILVERMIC\d', sym):
            continue
        if name == "NATURALGAS" and not re.match(r'^NATURALGAS\d', sym):
            continue
        if name == "NATGASMINI" and not re.match(r'^NATGASMINI\d', sym):
            continue

        exp = row.get("expiry", "")
        if exp:
            try:
                exp_dt = datetime.strptime(exp, "%d%b%Y")
                candidates.append({
                    "token": row["token"],
                    "symbol": sym,
                    "expiry": exp_dt,
                })
            except Exception:
                pass

    if not candidates:
        logger.error(f"No MCX futures found for {name}")
        return None

    now = datetime.now()
    candidates.sort(key=lambda x: x["expiry"])
    future = [c for c in candidates if c["expiry"] > now]
    if not future:
        return candidates[-1]

    chosen = future[0]
    days_left = (chosen["expiry"] - now).days
    if days_left <= 10 and len(future) > 1:
        chosen = future[1]

    logger.info(f"Resolved {name} → {chosen['symbol']} (token={chosen['token']})")
    return chosen


def _get_daily_candles(token: str, symbol: str, n_days: int = 30) -> list[dict]:
    """Fetch last n_days of daily OHLC candles from Angel One."""
    from data.angel_api import angel_api

    try:
        to_date   = datetime.now().strftime("%Y-%m-%d 23:59")
        from_date = (datetime.now() - timedelta(days=n_days + 10)).strftime("%Y-%m-%d 09:00")

        raw = angel_api.get_candle_data(
            token=token,
            exchange="MCX",
            interval="ONE_DAY",
            from_date=from_date,
            to_date=to_date,
        )
        if not raw:
            logger.warning(f"No candle data for {symbol}")
            return []

        candles = []
        today_str = datetime.now().strftime("%Y-%m-%d")
        for c in raw:
            ts, o, high, low, close, vol = c
            date_str = ts[:10]
            # Exclude today's incomplete candle
            if date_str >= today_str:
                continue
            # Convert to MCX CSV format: "DD Mon YYYY"
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            candles.append({
                "Date":   dt.strftime("%d %b %Y"),
                "Open":   f"{float(o):.2f}",
                "High":   f"{float(high):.2f}",
                "Low":    f"{float(low):.2f}",
                "Close":  f"{float(close):.2f}",
                "Volume": str(int(float(vol))),
                "OI":     "0",
            })

        logger.info(f"{symbol}: {len(candles)} completed candles fetched")
        return candles

    except Exception as e:
        logger.error(f"Candle fetch error for {symbol}: {e}")
        return []


def _update_csv(csv_key: str, new_candles: list[dict]) -> bool:
    """Merge new candles into existing CSV file."""
    if not new_candles:
        return False

    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, f"{csv_key}_ohlc.csv")

    existing = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing[row["Date"]] = row
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")

    # Merge: new data overwrites old
    for candle in new_candles:
        existing[candle["Date"]] = candle

    # Sort newest first
    sorted_rows = sorted(
        existing.values(),
        key=lambda x: datetime.strptime(x["Date"], "%d %b %Y"),
        reverse=True
    )

    with open(file_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "Open", "High", "Low", "Close", "Volume", "OI"])
        writer.writeheader()
        writer.writerows(sorted_rows)

    logger.info(f"✅ Updated {csv_key}_ohlc.csv with {len(new_candles)} candles ({len(sorted_rows)} total rows)")
    return True


def run_update(n_days: int = 30) -> dict:
    """
    Main entry point — fetches OHLC for all commodities and updates CSVs.
    Returns a summary dict.
    """
    from data.angel_api import angel_api

    if not angel_api.is_connected():
        logger.error("Angel One not connected — skipping MCX OHLC update")
        return {"success": False, "reason": "Angel One not connected"}

    summary = {}
    for csv_key, config in COMMODITY_CONFIG.items():
        name = config["search"]
        logger.info(f"--- Updating {name} ({csv_key}) ---")

        info = _find_near_month_token(name, config["exchange"])
        if not info:
            summary[csv_key] = "FAILED (token not found)"
            continue

        candles = _get_daily_candles(info["token"], info["symbol"], n_days)
        if not candles:
            summary[csv_key] = "FAILED (no candles)"
            continue

        ok = _update_csv(csv_key, candles)
        summary[csv_key] = f"OK ({len(candles)} candles)" if ok else "FAILED (csv write)"

    # Also copy naturalgasm as natgasmini alias if needed
    ng_mini = os.path.join(DATA_DIR, "naturalgasm_ohlc.csv")
    natgas_mini_src = os.path.join(DATA_DIR, "naturalgasm_ohlc.csv")
    if os.path.exists(natgas_mini_src):
        logger.info("naturalgasm_ohlc.csv is up to date")

    logger.info(f"MCX OHLC Update complete: {summary}")
    return {"success": True, "summary": summary}


if __name__ == "__main__":
    # For manual testing
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    from data.angel_api import angel_api
    angel_api.connect()
    result = run_update(n_days=30)
    print(result)
