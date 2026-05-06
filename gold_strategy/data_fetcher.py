"""
Data Fetcher — Fetches 4-day & 2-day OHLC from Angel One MCX candle API.
Instruments: GOLD, GOLDM
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
    "GOLD":  {"exchange": "MCX", "search": "GOLD",  "lots": 2},
    "GOLDM": {"exchange": "MCX", "search": "GOLDM", "lots": 2},
}


def _count_working_days(start: datetime, end: datetime) -> int:
    """Count Mon-Fri days between two dates (exclusive of start, inclusive of end)."""
    count = 0
    curr = start
    while curr < end:
        curr += timedelta(days=1)
        if curr.weekday() < 5:
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

        # Strict match: symbol must be EXACTLY our prefix then a digit
        # GOLD   → GOLD28APR26FUT  ✓   GOLDGUINEA, GOLDM, GOLDPETAL ✗
        # GOLDM  → GOLDM28APR26FUT ✓   GOLD alone                   ✗
        if name == "GOLD" and not re.match(r'^GOLD\d', sym):
            continue
        if name == "GOLDM" and not re.match(r'^GOLDM\d', sym):
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

    # Warn if the chosen contract wasn't actually near-month on the requested date
    # (happens when the true near-month has expired and been removed from master)
    if as_of_date and chosen_current["expiry"] > as_of_date + timedelta(days=45):
        logger.warning(
            f"{name}: ⚠️ Historical near-month contract for {as_of_date.date()} has EXPIRED and "
            f"is no longer in Angel One master. Using {chosen_current['trading_symbol']} instead — "
            f"prices may differ from the contract that was actually traded on that date."
        )

    logger.info(f"Resolved {name} CURRENT : {chosen_current['trading_symbol']} (token={chosen_current['token']}, expiry={chosen_current['expiry'].date()}, as_of={reference_date.date()})")
    if chosen_next:
        logger.info(f"Resolved {name} NEXT    : {chosen_next['trading_symbol']} (token={chosen_next['token']}, expiry={chosen_next['expiry'].date()})")
        
    return {
        "current": chosen_current,
        "next": chosen_next
    }


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
    
    # We use MCX CSV for fallback, but for dual-contract accuracy, we should rely on API if possible.
    # However, to preserve existing logic, we fetch CSV for current contract
    curr_candles = get_mcx_ohlc_from_csv(instrument, n_days=10)
    
    if len(curr_candles) < 4:
        logger.error(f"{instrument} (Current): Need at least 4 completed candles from MCX CSV, got {len(curr_candles)}")
        return None

    result = {
        "current": {
            "token":          curr_info["token"],
            "trading_symbol": curr_info["trading_symbol"],
            "lot_size":       int(curr_info["lot_size"]),
            "candles":        curr_candles,
            "expiry_date":    curr_info["expiry"],
        }
    }

    # Fetch for Next Contract (if in Rollover Window)
    if tokens_info.get("next"):
        next_info = tokens_info["next"]
        # Next contract MUST use Angel API because CSV only tracks near-month
        next_candles = _get_daily_candles(next_info["token"], next_info["trading_symbol"], n_days=10)
        if len(next_candles) >= 4:
            result["next"] = {
                "token":          next_info["token"],
                "trading_symbol": next_info["trading_symbol"],
                "lot_size":       int(next_info["lot_size"]),
                "candles":        next_candles,
                "expiry_date":    next_info["expiry"],
            }
        else:
            logger.warning(f"{instrument} (Next): Not enough candles from Angel API, disabling dual-mode.")

    return result


def get_ltp(token: str, symbol: str, exchange: str = "MCX") -> float | None:
    """Get live last-traded price."""
    return angel_api.get_ltp(exchange, symbol, token)
