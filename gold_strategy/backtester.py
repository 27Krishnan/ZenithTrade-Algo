"""
Gold Strategy Backtester - Multi-Day Simulation
===============================================
Given an instrument and a START date (the day we would have ENTERED the trade):
  1. Fetch 4 completed daily candles BEFORE that date -> H4/L4/H2/L2
  2. Calculate all strategy levels (Entry, Target-Lot1, SL1, SL2)
  3. Fetch 1-minute intraday candles FOR the start date
  4. Check the opening gap window; if OK enter after the 10-minute lock
  5. After Lot-1 Target is hit -> Lot-1 CLOSED, record event
  6. Continue with Lot-2 only using ROLLING SL2 that updates daily
  7. Return full multi-day timeline of events

Key P&L Units:
  - GOLD/GOLDM backtest stays in raw strategy points for display
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
import time
from loguru import logger
from .calculator import GoldLevels, rt
from .data_fetcher import _find_near_month_token
from core.mcx_data import get_mcx_ohlc_from_csv

LOTS = 2
MAX_SIMULATION_DAYS = 20

MULTIPLIERS = {
    "GOLD": 100,
    "GOLDM": 10
}


def _snapshot_levels(gl: GoldLevels, e_l, e_s, t_l, t_s, sl1_l, sl1_s, sl2_l, sl2_s) -> dict:
    levels = gl.to_dict()
    levels["e_l"] = e_l
    levels["e_s"] = e_s
    levels["t_l"] = t_l
    levels["t_s"] = t_s
    if levels.get("sl1_long"):
        levels["sl1_long"]["sl"] = sl1_l
    if levels.get("sl1_short"):
        levels["sl1_short"]["sl"] = sl1_s
    if levels.get("sl2_long"):
        levels["sl2_long"]["sl"] = sl2_l
    if levels.get("sl2_short"):
        levels["sl2_short"]["sl"] = sl2_s
    return levels


def _working_days_from(start_date: datetime, n: int) -> list[datetime]:
    """Generate n calendar dates from start (Mon-Fri only, max 60-day window)."""
    result = []
    d = start_date
    limit = 0
    while len(result) < n and limit < 60:
        if d.weekday() < 5:
            result.append(d)
        d += timedelta(days=1)
        limit += 1
    return result


def run_backtest(instrument: str, date_str: str) -> dict:
    """
    Main entry. date_str = "YYYY-MM-DD" = the day we want to simulate entry.
    """
    try:
        from data.angel_api import angel_api
    except ImportError:
        return {"error": "Angel One API not available"}

    if not angel_api.is_connected():
        return {"error": "Angel One not connected"}

    target_date = datetime.strptime(date_str, "%Y-%m-%d")

    tokens_info = _find_near_month_token(instrument, as_of_date=target_date)
    if not tokens_info or not tokens_info.get("current"):
        return {"error": f"Could not resolve {instrument} contract"}
        
    info = tokens_info["current"]

    contract_warning = None
    from datetime import timedelta as _td
    if info["expiry"] > target_date + _td(days=45):
        contract_warning = (
            f"DATA LIMITATION: The near-month contract for {date_str} was probably "
            f"GOLD...{target_date.strftime('%b%y').upper()}FUT and has already expired. "
            f"Using {info['trading_symbol']} instead, so prices may differ from the "
            f"historical near-month chart."
        )

    # 2. Fetch daily candles ending before entry date ───────────────
    all_history = get_mcx_ohlc_from_csv(instrument, n_days=30, before_date=date_str, expiry_date=info.get("expiry"))
    
    if not all_history:
        return {"error": f"No historical daily data found for {instrument} in CSV before {date_str}"}

    if len(all_history) < 4:
        return {"error": f"Need >=4 completed days before {date_str}, got {len(all_history)}"}

    used_days = all_history[:4]
    gl = GoldLevels(
        instrument=instrument,
        trading_symbol=info["trading_symbol"],
        token=info["token"],
        raw_days=used_days,
    )

    simulation = _simulate_multiday(
        gl=gl,
        info=info,
        entry_date=target_date,
        all_history=all_history,
        angel_api=angel_api,
    )

    return {
        "instrument": instrument,
        "date": date_str,
        "trading_symbol": info["trading_symbol"],
        "contract_expiry": info["expiry"].strftime("%Y-%m-%d"),
        "contract_warning": contract_warning,
        "used_days": used_days,
        "levels": gl.to_dict(),
        "simulation": simulation,
    }


def _parse_daily(raw: list, exclude_date: str | None = None) -> list[dict]:
    """Angel One daily candle -> list of dicts, newest first, excluding today."""
    out = []
    for c in raw:
        ts, o, h, l, close, vol = c
        d = ts[:10]
        if exclude_date and d >= exclude_date:
            continue
        out.append({
            "date": d,
            "high": float(h),
            "low": float(l),
            "open": float(o),
            "close": float(close),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _fetch_intraday(info: dict, date: datetime, angel_api) -> list[dict]:
    """Fetch 1-minute candles for a single date, returns [] if none."""
    date_str = date.strftime("%Y-%m-%d")
    raw = angel_api.get_candle_data(
        token=info["token"],
        exchange="MCX",
        interval="ONE_MINUTE",
        from_date=f"{date_str} 09:00",
        to_date=f"{date_str} 23:59",
    )
    time.sleep(0.5)
    if not raw:
        return []
    return [
        {
            "time": c[0][11:16],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        }
        for c in raw if c[0][:10] == date_str
    ]


def _recalc_sl2(gl: GoldLevels, history: list[dict], direction: str, actual_entry: float) -> tuple[float, str]:
    """
    Recompute SL2 from the current trailing 4-day window.
    Returns (new_sl, description_of_new_window)
    """
    four = history[:4]
    if len(four) < 4:
        if direction == "long":
            return gl.sl2_long, "unchanged"
        return gl.sl2_short, "unchanged"

    h4 = max(d["high"] for d in four)
    l4 = min(d["low"] for d in four)
    dates_used = " / ".join(d["date"][5:] for d in four)

    if direction == "long":
        pt_a = rt(actual_entry * 0.985)
        pt_b = rt(l4 * 0.9988)
        new_sl = max(pt_a, pt_b)
        return new_sl, f"Window [{dates_used}] | L4={l4:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL2={new_sl:.2f}"

    pt_a = rt(actual_entry * 1.015)
    pt_b = rt(h4 * 1.0012)
    new_sl = min(pt_a, pt_b)
    return new_sl, f"Window [{dates_used}] | H4={h4:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL2={new_sl:.2f}"


def _recalc_sl1(gl: GoldLevels, history: list[dict], direction: str, actual_entry: float) -> tuple[float, str]:
    """Recalculate SL1 (Phase 1) using 2-day rolling window."""
    two = history[:2]
    if len(two) < 2:
        return 0, "Not enough history"

    h2 = max(d["high"] for d in two)
    l2 = min(d["low"] for d in two)
    dates_used = " / ".join(d["date"][5:] for d in two)

    if direction == "long":
        pt_a = rt(actual_entry * 0.985)
        pt_b = rt(l2 * 0.9988)
        new_sl = max(pt_a, pt_b)
        return new_sl, f"Window [{dates_used}] | L2={l2:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL1={new_sl:.2f}"

    pt_a = rt(actual_entry * 1.015)
    pt_b = rt(h2 * 1.0012)
    new_sl = min(pt_a, pt_b)
    return new_sl, f"Window [{dates_used}] | H2={h2:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL1={new_sl:.2f}"


def _simulate_multiday(
    gl: GoldLevels,
    info: dict,
    entry_date: datetime,
    all_history: list[dict],
    angel_api,
) -> dict:
    """Replay the strategy across multiple intraday sessions."""
    e_l = gl.e_l
    e_s = gl.e_s
    t_l = gl.t_l
    t_s = gl.t_s
    sl1_l = gl.sl1_long
    sl1_s = gl.sl1_short
    sl2_l = gl.sl2_long
    sl2_s = gl.sl2_short

    long_state = "PENDING"
    short_state = "PENDING"
    long_entry = long_exit = long_reason = None
    short_entry = short_exit = short_reason = None
    long_entry_date = long_exit_date = None
    short_entry_date = short_exit_date = None
    long_lot1_pnl = None
    long_pnl = None
    short_lot1_pnl = None
    short_pnl = None
    rollover_pnl_l = 0
    rollover_pnl_s = 0
    mult = MULTIPLIERS.get(gl.instrument, 100)
    events = []

    def ev(day: str, t: str, msg: str):
        events.append({"date": day, "time": t, "event": msg})

    rolling_history = list(all_history)
    sim_date = entry_date
    days_simulated = []
    last_close = None

    today_dt = datetime.now()
    for day_num in range(MAX_SIMULATION_DAYS):
        # 🛑 Stop if we reached future dates
        if sim_date > today_dt:
            break
            
        if long_state in ("CLOSED", "GAP") and short_state in ("CLOSED", "GAP", "PENDING"):
            break
        if long_state == "PENDING" and short_state == "PENDING" and day_num > 0:
            break

        day_str = sim_date.strftime("%Y-%m-%d")
        intraday = _fetch_intraday(info, sim_date, angel_api)
        if not intraday and day_num == 0:
            return {
                "error": f"No 1-minute data for {day_str} - market may have been closed",
                "events": events,
            }

        days_simulated.append(day_str)

        if day_num > 0:
            if long_state == "ACTIVE_P2":
                new_sl, desc = _recalc_sl2(gl, rolling_history, "long", long_entry)
                if new_sl > sl2_l:
                    ev(day_str, "09:10", f"LONG SL2 TRAILING UP: {sl2_l:.2f} -> {new_sl:.2f} | {desc}")
                    sl2_l = new_sl
                else:
                    ev(day_str, "09:10", f"LONG SL2 UNCHANGED: {sl2_l:.2f} | {desc}")

            if short_state == "ACTIVE_P2":
                new_sl, desc = _recalc_sl2(gl, rolling_history, "short", short_entry)
                if new_sl < sl2_s:
                    ev(day_str, "09:10", f"SHORT SL2 TRAILING DOWN: {sl2_s:.2f} -> {new_sl:.2f} | {desc}")
                    sl2_s = new_sl
                else:
                    ev(day_str, "09:10", f"SHORT SL2 UNCHANGED: {sl2_s:.2f} | {desc}")

            if long_state == "ACTIVE_P1":
                new_sl, desc = _recalc_sl1(gl, rolling_history, "long", long_entry)
                if new_sl > sl1_l:
                    ev(day_str, "09:10", f"LONG SL1 TRAILING UP: {sl1_l:.2f} -> {new_sl:.2f} | {desc}")
                    sl1_l = new_sl
                else:
                    ev(day_str, "09:10", f"LONG (2 Lots) active from {long_entry:.2f} | SL1={sl1_l:.2f} | {desc}")

            if short_state == "ACTIVE_P1":
                new_sl, desc = _recalc_sl1(gl, rolling_history, "short", short_entry)
                if new_sl < sl1_s:
                    ev(day_str, "09:10", f"SHORT SL1 TRAILING DOWN: {sl1_s:.2f} -> {new_sl:.2f} | {desc}")
                    sl1_s = new_sl
                else:
                    ev(day_str, "09:10", f"SHORT (2 Lots) active from {short_entry:.2f} | SL1={sl1_s:.2f} | {desc}")

        start_time_str = intraday[0]["time"] if intraday else "09:00"

        def add_mins(time_str, mins):
            hh, mm = map(int, time_str.split(":"))
            total = hh * 60 + mm + mins
            return f"{total // 60:02d}:{total % 60:02d}"

        wait_until_entry = add_mins(start_time_str, 10) # 09:10
        gap_until = add_mins(start_time_str, 10)       # 09:10
        recovery_t = add_mins(start_time_str, 15)      # 09:15

        last_close = None
        gap_high = 0.0
        gap_low = 999999.0

        for c in intraday:
            t = c["time"]
            h, l = c["high"], c["low"]
            last_close = c["close"]

            if t < recovery_t:
                gap_high = max(gap_high, h)
                gap_low = min(gap_low, l)

            # --- Session Priority Logic (Exclusive Gap Monitoring) ---
            is_morning = ("09:00" <= start_time_str <= "10:00")
            is_evening = ("16:30" <= start_time_str <= "17:30")
            
            # Gap window for whichever session started first
            in_gap_window = (start_time_str <= t < gap_until)

            if day_num == 0 and in_gap_window:
                if long_state == "PENDING" and e_l and h >= e_l:
                    long_state = "GAP"
                    session_name = "MORNING" if is_morning else "EVENING"
                    ev(day_str, t, f"{session_name} GAP UP - Long Entry {e_l:.2f} breached. Waiting for 15-min recovery...")
                if short_state == "PENDING" and e_s and l <= e_s:
                    short_state = "GAP"
                    session_name = "MORNING" if is_morning else "EVENING"
                    ev(day_str, t, f"{session_name} GAP DOWN - Short Entry {e_s:.2f} breached. Waiting for 15-min recovery...")
                
            # --- Rule: HOLDING TARGETS active at 09:00 AM (Only during first 10 mins) ---
            if in_gap_window:
                if long_state == "ACTIVE_P1" and h >= t_l:
                    long_lot1_pnl = round((t_l - long_entry) * mult, 2)
                    long_state = "ACTIVE_P2"
                    ev(day_str, t, f"🎯 LONG LOT-1 TARGET HIT @ {t_l:.2f} (GAP HIT) | Lot-1 CLOSED | Lot-2 continues")
                elif long_state == "ACTIVE_P2" and l <= sl2_l:
                    exit_price = min(l, sl2_l); lot2_pnl = round((exit_price - long_entry) * mult, 2)
                    long_pnl = round((long_lot1_pnl or 0) + lot2_pnl, 2); long_state = "CLOSED"
                    ev(day_str, t, f"🏁 LONG LOT-2 SL2 HIT @ {exit_price:.2f} (GAP HIT)")

                if short_state == "ACTIVE_P1" and l <= t_s:
                    short_lot1_pnl = round((short_entry - t_s) * mult, 2)
                    short_state = "ACTIVE_P2"
                    ev(day_str, t, f"🎯 SHORT LOT-1 TARGET HIT @ {t_s:.2f} (GAP HIT) | Lot-1 CLOSED | Lot-2 continues")
                elif short_state == "ACTIVE_P2" and h >= sl2_s:
                    exit_price = max(h, sl2_s); lot2_pnl = round((short_entry - exit_price) * mult, 2)
                    short_pnl = round((short_lot1_pnl or 0) + lot2_pnl, 2); short_state = "CLOSED"
                    ev(day_str, t, f"🏁 SHORT LOT-2 SL2 HIT @ {exit_price:.2f} (GAP HIT)")

            if t < wait_until_entry:
                continue

            if day_num == 0 and t == recovery_t:
                if long_state == "GAP":
                    new_e_l = rt(gap_high * 1.0012)
                    new_t_l = rt(new_e_l * 1.015)
                    pt_a_l1 = rt(new_e_l * 0.985)
                    sl1_l = max(pt_a_l1, gl.sl1_long_b)
                    pt_a_l2 = rt(new_e_l * 0.985)
                    sl2_l = max(pt_a_l2, gl.sl2_long_b)
                    e_l = new_e_l
                    t_l = new_t_l
                    ev(day_str, t, f"GAP RECOVERY (LONG): New Entry L={e_l:.2f} | Target L={t_l:.2f} | SL1 L={sl1_l:.2f}")
                    long_state = "PENDING"

                if short_state == "GAP":
                    new_e_s = rt(gap_low * 0.9988)
                    new_t_s = rt(new_e_s * 0.985)
                    pt_a_s1 = rt(new_e_s * 1.015)
                    sl1_s = min(pt_a_s1, gl.sl1_short_b)
                    pt_a_s2 = rt(new_e_s * 1.015)
                    sl2_s = min(pt_a_s2, gl.sl2_short_b)
                    e_s = new_e_s
                    t_s = new_t_s
                    ev(day_str, t, f"GAP RECOVERY (SHORT): New Entry S={e_s:.2f} | Target S={t_s:.2f} | SL1 S={sl1_s:.2f}")
                    short_state = "PENDING"

            wait_until_sl = add_mins(start_time_str, 15)
            if t == wait_until_sl and day_num > 0:
                if long_state in ("ACTIVE_P1", "ACTIVE_P2"):
                    curr_sl = sl1_l if long_state == "ACTIVE_P1" else sl2_l
                    if l <= curr_sl:
                        new_sl = rt(gap_low * 0.9988)
                        if long_state == "ACTIVE_P1": sl1_l = new_sl
                        else: sl2_l = new_sl
                        ev(day_str, t, f"🔄 LONG SL GAP RESET (9:15 AM): New SL = {new_sl:.2f} (Low - 0.12%)")
                if short_state in ("ACTIVE_P1", "ACTIVE_P2"):
                    curr_sl = sl1_s if short_state == "ACTIVE_P1" else sl2_s
                    if h >= curr_sl:
                        new_sl = rt(gap_high * 1.0012)
                        if short_state == "ACTIVE_P1": sl1_s = new_sl
                        else: sl2_s = new_sl
                        ev(day_str, t, f"🔄 SHORT SL GAP RESET (9:15 AM): New SL = {new_sl:.2f} (High + 0.12%)")

            # wait_until_entry (09:10) is handled above by continue

            if day_str == info["expiry"].strftime("%Y-%m-%d") and t == "22:30":
                if long_state in ("ACTIVE_P1", "ACTIVE_P2") or short_state in ("ACTIVE_P1", "ACTIVE_P2"):
                    new_tokens = _find_near_month_token(gl.instrument, as_of_date=sim_date)
                    if new_tokens and new_tokens.get("next"):
                        next_info = new_tokens["next"]
                        next_intraday = _fetch_intraday(next_info, sim_date, angel_api)
                        next_ltp = None
                        for nc in next_intraday:
                            if nc["time"] >= "22:30":
                                next_ltp = nc["close"]
                                break
                        if not next_ltp and next_intraday:
                            next_ltp = next_intraday[-1]["close"]
                            
                        if next_ltp:
                            ev(day_str, t, f"⚠️ EXPIRY 1-HOUR WARNING: Initiating dual-contract rollover from {info['trading_symbol']} to {next_info['trading_symbol']}")
                            if long_state in ("ACTIVE_P1", "ACTIVE_P2"):
                                lots_rem = LOTS if long_state == "ACTIVE_P1" else 1
                                close_pnl = round((last_close - long_entry) * lots_rem * mult, 2)
                                rollover_pnl_l += close_pnl
                                ev(day_str, t, f"🔄 LONG ROLLOVER: Closed {info['trading_symbol']} at {last_close:.2f} (PnL booked: ₹{close_pnl:+.2f}). Re-entering LONG in {next_info['trading_symbol']} at {next_ltp:.2f}")
                                long_entry = next_ltp
                            
                            if short_state in ("ACTIVE_P1", "ACTIVE_P2"):
                                lots_rem = LOTS if short_state == "ACTIVE_P1" else 1
                                close_pnl = round((short_entry - last_close) * lots_rem * mult, 2)
                                rollover_pnl_s += close_pnl
                                ev(day_str, t, f"🔄 SHORT ROLLOVER: Closed {info['trading_symbol']} at {last_close:.2f} (PnL booked: ₹{close_pnl:+.2f}). Re-entering SHORT in {next_info['trading_symbol']} at {next_ltp:.2f}")
                                short_entry = next_ltp
                                
                            info = next_info
                            from .data_fetcher import get_mcx_ohlc_from_csv
                            # Strict Rule: Use MCX CSV for ALL OHLC data, including next contract during rollover!
                            new_hist = get_mcx_ohlc_from_csv(gl.instrument, n_days=30, before_date=day_str, expiry_date=info["expiry"])
                            if new_hist and len(new_hist) >= 4:
                                rolling_history = new_hist[:4]
                                if long_state == "ACTIVE_P1":
                                    sl1_l, desc = _recalc_sl1(gl, rolling_history, "long", long_entry)
                                    ev(day_str, t, f"   -> New LONG SL1 calculated natively using Next Contract History: {sl1_l:.2f} | {desc}")
                                elif long_state == "ACTIVE_P2":
                                    sl2_l, desc = _recalc_sl2(gl, rolling_history, "long", long_entry)
                                    ev(day_str, t, f"   -> New LONG SL2 calculated natively using Next Contract History: {sl2_l:.2f} | {desc}")
                                    
                                if short_state == "ACTIVE_P1":
                                    sl1_s, desc = _recalc_sl1(gl, rolling_history, "short", short_entry)
                                    ev(day_str, t, f"   -> New SHORT SL1 calculated natively using Next Contract History: {sl1_s:.2f} | {desc}")
                                elif short_state == "ACTIVE_P2":
                                    sl2_s, desc = _recalc_sl2(gl, rolling_history, "short", short_entry)
                                    ev(day_str, t, f"   -> New SHORT SL2 calculated natively using Next Contract History: {sl2_s:.2f} | {desc}")
                            else:
                                ev(day_str, t, "   -> Failed to fetch history for new contract, SL values remain unchanged!")

            if day_num == 0:
                if long_state == "PENDING" and e_l and h >= e_l:
                    long_state = "ACTIVE_P1"; long_entry = e_l; long_entry_date = day_str
                    ev(day_str, t, f"🟢 LONG ENTRY (2 Lots) @ {e_l:.2f} | SL1={sl1_l:.2f} Lot-1 Target={t_l:.2f}")
                if short_state == "PENDING" and e_s and l <= e_s:
                    short_state = "ACTIVE_P1"; short_entry = e_s; short_entry_date = day_str
                    ev(day_str, t, f"🔴 SHORT ENTRY (2 Lots) @ {e_s:.2f} | SL1={sl1_s:.2f} Lot-1 Target={t_s:.2f}")

            if long_state == "ACTIVE_P1":
                if l <= sl1_l:
                    exit_price = min(l, sl1_l)
                    pts = exit_price - long_entry
                    long_pnl = round(pts * LOTS * mult + rollover_pnl_l, 2)
                    long_exit = exit_price; long_reason = "SL1_HIT"; long_exit_date = day_str
                    long_state = "CLOSED"
                    ev(day_str, t, f"📉 LONG SL1 HIT @ {exit_price:.2f}  |  Both lots closed  |  PnL=₹{long_pnl:+.2f}")
                elif h >= t_l:
                    long_lot1_pnl = round((t_l - long_entry) * mult, 2)
                    long_state = "ACTIVE_P2"
                    ev(day_str, t, f"🎯 LONG LOT-1 TARGET HIT @ {t_l:.2f}  |  Lot-1 CLOSED (PnL=₹{long_lot1_pnl:+.2f})  |  Lot-2 continues with trailing SL2={sl2_l:.2f}")

            if long_state == "ACTIVE_P2":
                if l <= sl2_l:
                    exit_price = min(l, sl2_l)
                    lot2_pnl = round((exit_price - long_entry) * mult, 2)
                    long_pnl = round((long_lot1_pnl or 0) + lot2_pnl + rollover_pnl_l, 2)
                    long_exit = exit_price; long_reason = "SL2_HIT"; long_exit_date = day_str
                    long_state = "CLOSED"
                    ev(day_str, t, f"🏁 LONG LOT-2 SL2 HIT @ {exit_price:.2f}  |  Lot-2 closed  |  Lot-2 PnL=₹{lot2_pnl:+.2f}  |  TOTAL=₹{long_pnl:+.2f}")

            if short_state == "ACTIVE_P1":
                if h >= sl1_s:
                    exit_price = max(h, sl1_s)
                    pts = short_entry - exit_price
                    short_pnl = round(pts * LOTS * mult + rollover_pnl_s, 2)
                    short_exit = exit_price; short_reason = "SL1_HIT"; short_exit_date = day_str
                    short_state = "CLOSED"
                    ev(day_str, t, f"📉 SHORT SL1 HIT @ {exit_price:.2f}  |  Both lots closed  |  PnL=₹{short_pnl:+.2f}")
                elif l <= t_s:
                    short_lot1_pnl = round((short_entry - t_s) * mult, 2)
                    short_state = "ACTIVE_P2"
                    ev(day_str, t, f"🎯 SHORT LOT-1 TARGET HIT @ {t_s:.2f}  |  Lot-1 CLOSED (PnL=₹{short_lot1_pnl:+.2f})  |  Lot-2 continues with trailing SL2={sl2_s:.2f}")

            if short_state == "ACTIVE_P2":
                if h >= sl2_s:
                    exit_price = max(h, sl2_s)
                    lot2_pnl = round((short_entry - exit_price) * mult, 2)
                    short_pnl = round((short_lot1_pnl or 0) + lot2_pnl + rollover_pnl_s, 2)
                    short_exit = exit_price; short_reason = "SL2_HIT"; short_exit_date = day_str
                    short_state = "CLOSED"
                    ev(day_str, t, f"🏁 SHORT LOT-2 SL2 HIT @ {exit_price:.2f} | Lot-2 closed | Lot-2 PnL=₹{lot2_pnl:+.2f} | TOTAL=₹{short_pnl:+.2f}")

            if long_state == "CLOSED" and short_state in ("CLOSED", "GAP", "PENDING"):
                break
            if short_state == "CLOSED" and long_state in ("CLOSED", "GAP", "PENDING"):
                break

        if last_close and intraday:
            day_candle = {
                "date": day_str,
                "high": max(c["high"] for c in intraday),
                "low": min(c["low"] for c in intraday),
                "open": intraday[0]["open"],
                "close": last_close,
            }
            rolling_history.insert(0, day_candle)

        sim_date += timedelta(days=1)
        while sim_date.weekday() >= 5:
            sim_date += timedelta(days=1)

    if last_close:
        if long_state in ("ACTIVE_P1", "ACTIVE_P2") and long_entry:
            live_pts = last_close - long_entry
            lots_rem = LOTS if long_state == "ACTIVE_P1" else 1
            long_pnl = round((long_lot1_pnl or 0) + (live_pts * lots_rem * mult) + rollover_pnl_l, 2)
        if short_state in ("ACTIVE_P1", "ACTIVE_P2") and short_entry:
            live_pts = short_entry - last_close
            lots_rem = LOTS if short_state == "ACTIVE_P1" else 1
            short_pnl = round((short_lot1_pnl or 0) + (live_pts * lots_rem * mult) + rollover_pnl_s, 2)

    effective_levels = _snapshot_levels(gl, e_l, e_s, t_l, t_s, sl1_l, sl1_s, sl2_l, sl2_s)

    return {
        "long_state": long_state,
        "short_state": short_state,
        "long_entry": long_entry,
        "short_entry": short_entry,
        "long_lot1_pnl": long_lot1_pnl,
        "short_lot1_pnl": short_lot1_pnl,
        "long_entry_date": long_entry_date,
        "long_exit_date": long_exit_date,
        "short_entry_date": short_entry_date,
        "short_exit_date": short_exit_date,
        "long_exit": long_exit,
        "short_exit": short_exit,
        "long_reason": long_reason,
        "short_reason": short_reason,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "last_price": last_close,
        "final_sl2_long": sl2_l,
        "final_sl2_short": sl2_s,
        "events": events,
        "effective_levels": effective_levels,
        "initial_levels": {
            "h3": gl.h4, "l3": gl.l4,
            "h2": gl.h2, "l2": gl.l2,
            "e_l": gl.e_l, "e_s": gl.e_s,
            "t_l": gl.t_l, "t_s": gl.t_s,
            "sl1_l": gl.sl1_long, "sl1_s": gl.sl1_short,
            "sl2_l": gl.sl2_long, "sl2_s": gl.sl2_short,
            "dates_used": [d["date"] for d in gl.raw_days]
        }
    }
