"""
Data Fetcher — Fetches 4-day & 2-day OHLC from NSE CSV strictly.
Instruments: NIFTY
"""
import sys
import os
from datetime import datetime, timedelta
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.angel_api import angel_api
from config.settings import settings
from core.nse_data import get_nse_ohlc_from_csv

INSTRUMENTS = {
    "NIFTY": {"exchange": "NFO", "search": "NIFTY", "lots": 1},
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
    """Search instrument master for nearest-expiry NFO futures contract."""
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
    candidates = []
    for row in data:
        sym  = row.get("symbol", "")
        exch = row.get("exch_seg", "")
        inst = row.get("instrumenttype", "")
        
        if exch != "NFO" or inst != "FUTIDX":
            continue

        if name == "NIFTY" and not re.match(r'^NIFTY\d', sym):
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
        logger.error(f"No NFO futures found for {name}")
        return None

    reference_date = as_of_date if as_of_date else datetime.now()
    candidates.sort(key=lambda x: x["expiry"])

    future = [c for c in candidates if c["expiry"] > reference_date]
    if not future:
        chosen_current = candidates[-1]
        chosen_next = None
    else:
        nearest = future[0]
        days_left = _count_working_days(reference_date, nearest["expiry"])
        chosen_current = nearest
        
        # 10 Trading Days Rollover Rule
        if days_left <= 10 and len(future) > 1:
            chosen_next = future[1]
        else:
            chosen_next = None

    if as_of_date and chosen_current["expiry"] > as_of_date + timedelta(days=45):
        logger.warning(f"{name}: Historical contract expired. Using {chosen_current['trading_symbol']}")

    logger.info(f"Resolved {name} CURRENT : {chosen_current['trading_symbol']} (token={chosen_current['token']}, expiry={chosen_current['expiry'].date()})")
    if chosen_next:
        logger.info(f"Resolved {name} NEXT    : {chosen_next['trading_symbol']} (token={chosen_next['token']}, expiry={chosen_next['expiry'].date()})")
        
    return {
        "current": chosen_current,
        "next": chosen_next
    }


def fetch_instrument_data(instrument: str) -> dict | None:
    """
    Full pipeline: resolve tokens → fetch candles for BOTH contracts (if applicable) → return dict.
    Returns None on failure.
    """
    if not angel_api.is_connected():
        logger.warning("Angel One not connected — cannot fetch data")
        return None

    tokens_info = _find_near_month_token(instrument)
    if not tokens_info or not tokens_info.get("current"):
        return None

    # Fetch for Current Contract
    curr_info = tokens_info["current"]
    
    # STRICT RULE ENFORCEMENT: Only use NSE CSV for OHLC data. 
    # Never use Angel One for historical Open/High/Low/Close.
    nse_candles = get_nse_ohlc_from_csv(instrument, n_days=10)
    
    if len(nse_candles) < 4:
        logger.error(f"{instrument} (Current): Need at least 4 completed candles from NSE CSV, got {len(nse_candles)}")
        return None

    result = {
        "current": {
            "token":          curr_info["token"],
            "trading_symbol": curr_info["trading_symbol"],
            "lot_size":       int(curr_info["lot_size"]),
            "candles":        nse_candles,
            "expiry_date":    curr_info["expiry"],
        }
    }

    # Fetch for Next Contract (if in Rollover Window)
    if tokens_info.get("next"):
        next_info = tokens_info["next"]
        # STRICT RULE: Use the same NSE CSV data for the next contract.
        result["next"] = {
            "token":          next_info["token"],
            "trading_symbol": next_info["trading_symbol"],
            "lot_size":       int(next_info["lot_size"]),
            "candles":        nse_candles,
            "expiry_date":    next_info["expiry"],
        }

    return result


def get_ltp(token: str, symbol: str, exchange: str = "NFO") -> float | None:
    """Get live last-traded price."""
    return angel_api.get_ltp(exchange, symbol, token)
    """Get live last-traded price."""
    return angel_api.get_ltp(exchange, symbol, token)
