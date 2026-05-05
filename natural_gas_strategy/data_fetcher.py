"""
Data Fetcher — Fetches 4-day & 2-day OHLC from Angel One MCX candle API.
Instruments: SILVER, SILVERM, SILVERMIC
"""
import sys
import os
from datetime import datetime, timedelta
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.angel_api import angel_api
from config.settings import settings
from core.mcx_data import get_mcx_ohlc_from_csv

# Instrument definitions — we'll resolve these to current near-month contracts
INSTRUMENTS = {
    "NATURALGAS":    {"exchange": "MCX", "search": "NATURALGAS",    "lots": 2},
    "NATURALGASM":   {"exchange": "MCX", "search": "NATGASMINI",   "lots": 2},
}

def _count_working_days(start: datetime, end: datetime) -> int:
    """Count Mon-Fri days between two dates (exclusive of start, inclusive of end)."""
    count = 0
    curr = start
    while curr < end:
        curr += timedelta(days=1)
        if curr.weekday() < 5:  # 0=Mon ... 4=Fri
            count += 1
    return count

def _find_near_month_token(name: str, as_of_date: datetime | None = None) -> dict | None:
    """Search instrument master for nearest-expiry MCX futures contract."""
    try:
        from api.option_chain import load_master
        data = load_master()
        if not data:
            logger.error(f"No instrument master data available for {name}")
            return None
    except Exception as e:
        logger.error(f"Instrument master load failed: {e}")
        return None

    import re
    prefix = name.upper()
    candidates = []
    for row in data:
        sym  = row.get("symbol", "")
        exch = row.get("exch_seg", "")
        inst = row.get("instrumenttype", "")
        if exch != "MCX" or inst != "FUTCOM":
            continue

        if name == "NATURALGAS":
            if not re.match(r'^NATURALGAS\d', sym): continue
        elif name == "NATURALGASM":
            if not re.match(r'^NATGASMINI\d', sym): continue
        else:
            continue

        exp = row.get("expiry", "")
        if exp:
            try:
                exp_dt = datetime.strptime(exp, "%d%b%Y")
                candidates.append({
                    "token":          row["token"],
                    "trading_symbol": sym,
                    "expiry":         exp_dt,
                    "lot_size":       row.get("lotsize", "1"),
                })
            except Exception:
                pass

    if not candidates:
        logger.error(f"No MCX futures found for {name}")
        return None

    reference_date = as_of_date if as_of_date else datetime.now()
    candidates.sort(key=lambda x: x["expiry"])

    future = [c for c in candidates if c["expiry"] > reference_date]
    if not future:
        chosen = candidates[-1]
    else:
        nearest = future[0]
        days_left = _count_working_days(reference_date, nearest["expiry"])
        if days_left <= 10 and len(future) > 1:
            chosen = future[1]
        else:
            chosen = nearest

    if as_of_date and chosen["expiry"] > as_of_date + timedelta(days=45):
        logger.warning(f"{name}: Historical contract expired. Using {chosen['trading_symbol']}")

    logger.info(f"Resolved {name} : {chosen['trading_symbol']} (token={chosen['token']}, expiry={chosen['expiry'].date()})")
    return chosen


def _get_daily_candles(token: str, symbol: str, n_days: int = 7) -> list[dict]:
    """Fetch last n_days of daily candles from Angel One."""
    try:
        to_date   = datetime.now().strftime("%Y-%m-%d 23:59")
        from_date = (datetime.now() - timedelta(days=n_days + 5)).strftime("%Y-%m-%d 09:00")
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
        for c in raw:
            # Angel One format: [timestamp, open, high, low, close, volume]
            ts, o, high, low, close, vol = c
            date_str = ts[:10]  # "YYYY-MM-DD"
            candles.append({"date": date_str, "high": float(high), "low": float(low),
                             "open": float(o), "close": float(close)})
        # Sort newest first; exclude today's incomplete candle
        today_str = datetime.now().strftime("%Y-%m-%d")
        candles = [c for c in candles if c["date"] < today_str]
        candles.sort(key=lambda x: x["date"], reverse=True)
        # Keep only completed trading days
        logger.info(f"{symbol}: {len(candles)} completed candles fetched")
        return candles
    except Exception as e:
        logger.error(f"Candle fetch error for {symbol}: {e}")
        return []


def fetch_instrument_data(instrument: str) -> dict | None:
    """
    Full pipeline: resolve token → fetch candles → return dict with levels data.
    Returns None on failure.
    """
    if not angel_api.is_connected():
        logger.warning("Angel One not connected — cannot fetch data")
        return None

    info = _find_near_month_token(instrument)
    if not info:
        return None

    # Use local MCX CSV for historical candles
    candles = get_mcx_ohlc_from_csv(instrument, n_days=10)
    
    if len(candles) < 4:
        logger.error(f"{instrument}: Need at least 4 completed candles from MCX CSV, got {len(candles)}")
        return None

    return {
        "token":          info["token"],
        "trading_symbol": info["trading_symbol"],
        "lot_size":       int(info["lot_size"]),
        "candles":        candles,   # newest first, already excludes today
    }


def get_ltp(token: str, symbol: str, exchange: str = "MCX") -> float | None:
    """Get live last-traded price."""
    return angel_api.get_ltp(exchange, symbol, token)
