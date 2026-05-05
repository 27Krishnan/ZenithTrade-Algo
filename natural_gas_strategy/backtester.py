"""
Silver Strategy Backtester — Multi-Day Simulation
================================================
Given an instrument and a START date (the day we would have ENTERED the trade):
  1. Fetch 3 completed daily candles BEFORE that date  → H3/L3/H2/L2
  2. Calculate all strategy levels (Entry, Target-Lot1, SL1, SL2)
  3. Fetch 1-minute intraday candles FOR the start date
  4. Check 9:00–9:10 gap window; if OK enter at 9:10 at E_L or E_S
  5. After Lot-1 Target is hit → Lot-1 CLOSED, record event
  6. Continue with Lot-2 only using ROLLING SL2 that updates daily:
     - Each new completed day shifts the 3-day window
     - New L3/H3 → new SL2 via max/min(Point_A, Point_B)
     - SL2 only moves in the favourable direction (never tightens against us)
  7. Return full multi-day timeline of events

Key P&L Units:
  - Multipliers: SILVER=30, SILVERM=5, SILVERMIC=1
  - Lot sizes are stored and P&L is calculated in Rupees (₹)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
import time
from loguru import logger
from .calculator import NaturalGasLevels, rt
from .data_fetcher import _find_near_month_token
from core.mcx_data import get_mcx_ohlc_from_csv

LOTS = 2
MAX_SIMULATION_DAYS = 20   # safety limit

MULTIPLIERS = {
    "NATURALGAS":  1250,
    "NATURALGASM": 250
}


def _working_days_from(start_date: datetime, n: int) -> list[datetime]:
    """Generate n calendar dates from start (Mon–Fri only, max 60-day window)."""
    result = []
    d = start_date
    limit = 0
    while len(result) < n and limit < 60:
        if d.weekday() < 5:   # Mon=0 … Fri=4
            result.append(d)
        d += timedelta(days=1)
        limit += 1
    return result


def run_backtest(instrument: str, date_str: str) -> dict:
    """
    Main entry.  date_str = "YYYY-MM-DD" = the day we want to simulate entry.
    """
    try:
        from data.angel_api import angel_api
    except ImportError:
        return {"error": "Angel One API not available"}

    if not angel_api.is_connected():
        return {"error": "Angel One not connected"}

    target_date = datetime.strptime(date_str, "%Y-%m-%d")

    # ── 1. Resolve contract — find the near-month AS OF the backtest date ────
    info = _find_near_month_token(instrument, as_of_date=target_date)
    if not info:
        return {"error": f"Could not resolve {instrument} contract"}

    # Detect if the historically-correct contract is unavailable (expired & removed)
    contract_warning = None
    from datetime import timedelta as _td
    if info["expiry"] > target_date + _td(days=45):
        contract_warning = (
            f"⚠️ DATA LIMITATION: The near-month contract for {date_str} was "
            f"probably SILVER...{target_date.strftime('%b%y').upper()}FUT which has EXPIRED "
            f"and is no longer in Angel One's instrument master. "
            f"Using {info['trading_symbol']} (far-month at the time) instead. "
            f"Prices will differ from TradingView SILVER1! chart by the contract spread. "
            f"For accurate backtest, run dates within the last 15–20 days (while near-month is still active)."
        )
    # ── 2. Fetch daily candles ending before entry date ───────────────
    all_history = get_mcx_ohlc_from_csv(instrument, n_days=30, before_date=date_str)
    
    if not all_history:
        return {"error": f"No historical daily data found for {instrument} in CSV before {date_str}"}
    if len(all_history) < 3:
        return {"error": f"Need ≥3 completed days before {date_str}, got {len(all_history)}"}

    used_days = all_history[:3]
    gl = NaturalGasLevels(
        instrument=instrument, trading_symbol=info["trading_symbol"],
        token=info["token"], raw_days=used_days,
    )

    # ── 3. Simulate (possibly multi-day) ────────────────────────────────
    simulation = _simulate_multiday(
        gl=gl,
        info=info,
        entry_date=target_date,
        all_history=all_history,
        angel_api=angel_api,
    )

    return {
        "instrument":        instrument,
        "date":              date_str,
        "trading_symbol":    info["trading_symbol"],
        "contract_expiry":   info["expiry"].strftime("%Y-%m-%d"),
        "contract_warning":  contract_warning,
        "used_days":         used_days,
        "levels":            gl.to_dict(),
        "simulation":        simulation,
    }


# ─── Internal helpers ──────────────────────────────────────────────────────────

def _parse_daily(raw: list, exclude_date: str | None = None) -> list[dict]:
    """Angel One daily candle → list of dicts, newest first, excluding today."""
    out = []
    for c in raw:
        ts, o, h, l, close, vol = c
        d = ts[:10]
        if exclude_date and d >= exclude_date:
            continue
        out.append({"date": d, "high": float(h), "low": float(l),
                    "open": float(o), "close": float(close)})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _fetch_intraday(info: dict, date: datetime, angel_api) -> list[dict]:
    """Fetch 1-minute candles for a single date, returns [] if none."""
    date_str = date.strftime("%Y-%m-%d")
    raw = angel_api.get_candle_data(
        token=info["token"], exchange="MCX",
        interval="ONE_MINUTE",
        from_date=f"{date_str} 09:00",
        to_date=f"{date_str} 23:59",
    )
    time.sleep(0.5)  # 🛡️ RATE LIMIT PROTECTION
    if not raw:
        return []
    return [
        {"time": c[0][11:16], "open": float(c[1]),
         "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
        for c in raw if c[0][:10] == date_str
    ]


def _recalc_sl2(gl: NaturalGasLevels, history: list[dict], direction: str, actual_entry: float) -> tuple[float, str]:
    """
    Recompute SL2 from the current trailing 3-day window.
    Returns (new_sl, description_of_new_window)
    direction: "long" | "short"
    """
    three = history[:3]
    if len(three) < 3:
        if direction == "long":
            return gl.sl2_long, "unchanged (history < 3)"
        return gl.sl2_short, "unchanged (history < 3)"

    h3 = max(d["high"] for d in three)
    l3 = min(d["low"]  for d in three)
    dates_used = " / ".join(d["date"][5:] for d in three)

    if direction == "long":
        pt_a = rt(actual_entry * 0.96)
        pt_b = rt(l3 * 0.996)
        new_sl = max(pt_a, pt_b)
        return new_sl, f"Window [{dates_used}] | L3={l3:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL2={new_sl:.2f}"
    else:
        pt_a = rt(actual_entry * 1.04)
        pt_b = rt(h3 * 1.004)
        new_sl = min(pt_a, pt_b)
        return new_sl, f"Window [{dates_used}] | H3={h3:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL2={new_sl:.2f}"


def _recalc_sl1(gl, history, direction, actual_entry):
    """Recalculate SL1 (Phase 1) using 2-day rolling window."""
    two = history[:2]
    if len(two) < 2: return 0, "Not enough history"
    
    h2 = max(d["high"] for d in two)
    l2 = min(d["low"]  for d in two)
    dates_used = " / ".join(d["date"][5:] for d in two)

    if direction == "long":
        pt_a = rt(actual_entry * 0.96)
        pt_b = rt(l2 * 0.996)
        new_sl = max(pt_a, pt_b)
        return new_sl, f"Window [{dates_used}] | L2={l2:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL1={new_sl:.2f}"
    else:
        pt_a = rt(actual_entry * 1.04)
        pt_b = rt(h2 * 1.004)
        new_sl = min(pt_a, pt_b)
        return new_sl, f"Window [{dates_used}] | H2={h2:.2f} | A={pt_a:.2f} B={pt_b:.2f} -> SL1={new_sl:.2f}"


def _snapshot_levels(gl, e_l, e_s, t_l, t_s, sl1_l, sl1_s, sl2_l, sl2_s):
    """
    Return the current effective levels snapshot.
    This is used by backtest UI + sync-to-live so recovered entries and
    trailed stop-loss values are preserved instead of falling back to
    the original pre-gap calculation.
    """
    levels = gl.to_dict()
    levels["e_l"] = e_l
    levels["e_s"] = e_s
    levels["t_l"] = t_l
    levels["t_s"] = t_s

    if levels.get("sl1_long"):
        levels["sl1_long"]["sl"] = sl1_l
        levels["sl1_long"]["a"] = rt(e_l * 0.96) if e_l else levels["sl1_long"].get("a", 0)
    if levels.get("sl1_short"):
        levels["sl1_short"]["sl"] = sl1_s
        levels["sl1_short"]["a"] = rt(e_s * 1.04) if e_s else levels["sl1_short"].get("a", 0)
    if levels.get("sl2_long"):
        levels["sl2_long"]["sl"] = sl2_l
        levels["sl2_long"]["a"] = rt(e_l * 0.96) if e_l else levels["sl2_long"].get("a", 0)
    if levels.get("sl2_short"):
        levels["sl2_short"]["sl"] = sl2_s
        levels["sl2_short"]["a"] = rt(e_s * 1.04) if e_s else levels["sl2_short"].get("a", 0)

    return levels


def _simulate_multiday(
    gl: NaturalGasLevels,
    info: dict,
    entry_date: datetime,
    all_history: list[dict],
    angel_api,
) -> dict:
    """
    Replay the strategy across multiple intraday sessions.

    State machine:
      PENDING  → (9:10 price crosses E_L/E_S)
      ACTIVE_P1 → both lots live, SL1 active
        → Gap detected → GAP (no trade)
        → SL1 hit      → CLOSED
        → Lot-1 Target hit → LOT1_HIT (Lot-1 booked) → ACTIVE_P2
      ACTIVE_P2 → only Lot-2 live, SL2  trailing daily
        → SL2 hit → CLOSED
    """
    e_l   = gl.e_l;   e_s   = gl.e_s
    t_l   = gl.t_l;   t_s   = gl.t_s
    sl1_l = gl.sl1_long;  sl1_s = gl.sl1_short
    sl2_l = gl.sl2_long;  sl2_s = gl.sl2_short   # initial; will trail

    mult = MULTIPLIERS.get(gl.instrument, 1250)

    long_state  = "PENDING"
    short_state = "PENDING"
    long_entry  = long_exit  = long_reason  = None
    short_entry = short_exit = short_reason = None
    long_entry_date = long_exit_date = None
    short_entry_date = short_exit_date = None
    long_lot1_pnl  = None   # P&L from Lot-1 close
    long_pnl  = None        # total (Lot-1 + Lot-2)
    short_lot1_pnl = None
    short_pnl = None
    events = []

    def ev(day: str, t: str, msg: str):
        events.append({"date": day, "time": t, "event": msg})

    # rolling history we use to recalculate SL2 each day
    rolling_history = list(all_history)    # will prepend completed days as we go

    # Simulate each trading day starting from entry_date
    sim_date = entry_date
    days_simulated = []

    today_dt = datetime.now()
    for day_num in range(MAX_SIMULATION_DAYS):
        # 🛑 Stop if we reached future dates
        if sim_date > today_dt:
            break
            
        if long_state in ("CLOSED", "GAP") and short_state in ("CLOSED", "GAP", "PENDING"):
            break
        if long_state == "PENDING" and short_state == "PENDING" and day_num > 0:
            break   # no entry ever triggered

        day_str = sim_date.strftime("%Y-%m-%d")
        intraday = _fetch_intraday(info, sim_date, angel_api)
        if not intraday and day_num == 0:
            return {
                "error": f"No 1-minute data for {day_str} — market may have been closed",
                "events": events,
            }

        days_simulated.append(day_str)

        # ─ If Phase 2 and this is NOT the entry day → update trailing SL2 ──
        # This mirrors the 9:10 AM live scheduler that recalculates and places
        # the updated SL order each morning. Label shows "09:10" to match timing.
        if day_num > 0:
            if long_state == "ACTIVE_P2":
                new_sl, desc = _recalc_sl2(gl, rolling_history, "long", long_entry)
                if new_sl > sl2_l:   # only trail upward for long
                    ev(day_str, "09:10", f"📈 LONG SL2 TRAILING UP: {sl2_l:.2f} -> {new_sl:.2f}  |  {desc}")
                    sl2_l = new_sl
                else:
                    ev(day_str, "09:10", f"🔒 LONG SL2 UNCHANGED: {sl2_l:.2f}  |  {desc}")

            if short_state == "ACTIVE_P2":
                new_sl, desc = _recalc_sl2(gl, rolling_history, "short", short_entry)
                if new_sl < sl2_s:   # only trail downward for short
                    ev(day_str, "09:10", f"📉 SHORT SL2 TRAILING DOWN: {sl2_s:.2f} -> {new_sl:.2f}  |  {desc}")
                    sl2_s = new_sl
                else:
                    ev(day_str, "09:10", f"🔒 SHORT SL2 UNCHANGED: {sl2_s:.2f}  |  {desc}")

            # ─ Phase 1: SL1 trailing using 2-day window ─
            if long_state == "ACTIVE_P1":
                new_sl, desc = _recalc_sl1(gl, rolling_history, "long", long_entry)
                if new_sl > sl1_l:
                    ev(day_str, "09:10", f"📈 LONG SL1 TRAILING UP: {sl1_l:.2f} -> {new_sl:.2f}  |  {desc}")
                    sl1_l = new_sl
                else:
                    ev(day_str, "09:10", f"🔄 LONG (2 Lots) active from {long_entry:.2f} | SL1={sl1_l:.2f} | {desc}")
            
            if short_state == "ACTIVE_P1":
                new_sl, desc = _recalc_sl1(gl, rolling_history, "short", short_entry)
                if new_sl < sl1_s:
                    ev(day_str, "09:10", f"📉 SHORT SL1 TRAILING DOWN: {sl1_s:.2f} -> {new_sl:.2f}  |  {desc}")
                    sl1_s = new_sl
                else:
                    ev(day_str, "09:10", f"🔄 SHORT (2 Lots) active from {short_entry:.2f} | SL1={sl1_s:.2f} | {desc}")

        # ─ Replay intraday candles ──────────────────────────────────────
        # --- Dynamic Session Timing ---
        # Find the start time of today's session from the first available candle
        start_time_str = intraday[0]["time"] if intraday else "09:00"
        
        # Helper to add minutes to HH:MM string
        def add_mins(time_str, m):
            hh, mm = map(int, time_str.split(':'))
            total = hh * 60 + mm + m
            return f"{total//60:02d}:{total%60:02d}"

        wait_until_entry = add_mins(start_time_str, 10) # 09:10
        gap_until  = add_mins(start_time_str, 10)       # 09:10
        recovery_t = add_mins(start_time_str, 15)       # 09:15

        last_close = None
        gap_high = 0.0; gap_low = 999999.0
        
        for c in intraday:
            t = c["time"]
            h, l = c["high"], c["low"]
            last_close = c["close"]

            # Track range for gap recovery
            if t < recovery_t:
                gap_high = max(gap_high, h)
                gap_low  = min(gap_low, l)

            # --- Session Priority Logic (Exclusive Gap Monitoring) ---
            is_morning = ("09:00" <= start_time_str <= "10:00")
            is_evening = ("16:30" <= start_time_str <= "17:30")
            
            # Gap window for whichever session started first
            in_gap_window = (start_time_str <= t < gap_until)

            if day_num == 0 and in_gap_window:
                if long_state == "PENDING" and e_l and h >= e_l:
                    long_state = "GAP"
                    session_name = "MORNING" if is_morning else "EVENING"
                    ev(day_str, t, f"⚠️ {session_name} GAP UP — Long Entry {e_l:.2f} breached. Waiting for 15-min recovery...")
                if short_state == "PENDING" and e_s and l <= e_s:
                    short_state = "GAP"
                    session_name = "MORNING" if is_morning else "EVENING"
                    ev(day_str, t, f"⚠️ {session_name} GAP DOWN — Short Entry {e_s:.2f} breached. Waiting for 15-min recovery...")
                
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

            # Gap Recovery Logic (15 mins after open)
            if day_num == 0 and t == recovery_t:
                # LONG Recovery
                if long_state == "GAP":
                    new_e_l = rt(gap_high * 1.004)
                    gl.update_from_actual_entry(new_e_l, "long")
                    e_l = gl.e_l; t_l = gl.t_l; sl1_l = gl.sl1_long; sl2_l = gl.sl2_long
                    ev(day_str, t, f"🔄 GAP RECOVERY (LONG): New Entry L={e_l:.2f} | Target L={t_l:.2f} | SL1 L={sl1_l:.2f}")
                    long_state = "PENDING"

                # SHORT Recovery
                if short_state == "GAP":
                    new_e_s = rt(gap_low * 0.996)
                    gl.update_from_actual_entry(new_e_s, "short")
                    e_s = gl.e_s; t_s = gl.t_s; sl1_s = gl.sl1_short; sl2_s = gl.sl2_short
                    ev(day_str, t, f"🔄 GAP RECOVERY (SHORT): New Entry S={e_s:.2f} | Target S={t_s:.2f} | SL1 S={sl1_s:.2f}")
                    short_state = "PENDING"

            # ALL days: skip any candle before wait_until (no SL/target action)
            # wait_until is now 09:15 to accommodate the new SL recovery rule
            wait_until_sl = add_mins(start_time_str, 15)
            
            if t == wait_until_sl and day_num > 0:
                # ── New 9:15 AM SL Reset Rule for Active Trades ──
                # If price is beyond SL at 9:15, reset SL instead of exiting.
                if long_state in ("ACTIVE_P1", "ACTIVE_P2"):
                    curr_sl = sl1_l if long_state == "ACTIVE_P1" else sl2_l
                    if l <= curr_sl:
                        new_sl = rt(gap_low * 0.996)
                        if long_state == "ACTIVE_P1": sl1_l = new_sl
                        else: sl2_l = new_sl
                        ev(day_str, t, f"🔄 LONG SL GAP RESET (9:15 AM): New SL = {new_sl:.2f} (Low - 0.4%)")
                
                if short_state in ("ACTIVE_P1", "ACTIVE_P2"):
                    curr_sl = sl1_s if short_state == "ACTIVE_P1" else sl2_s
                    if h >= curr_sl:
                        new_sl = rt(gap_high * 1.004)
                        if short_state == "ACTIVE_P1": sl1_s = new_sl
                        else: sl2_s = new_sl
                        ev(day_str, t, f"🔄 SHORT SL GAP RESET (9:15 AM): New SL = {new_sl:.2f} (High + 0.4%)")

            # wait_until_entry (09:10) is handled above by continue

            # ── Entry triggers (entry day only) ──────────────────────────
            if day_num == 0:
                if long_state == "PENDING" and e_l and h >= e_l:
                    long_state = "ACTIVE_P1"; long_entry = e_l; long_entry_date = day_str
                    ev(day_str, t, f"🟢 LONG ENTRY (2 Lots) @ {e_l:.2f}  |  SL1={sl1_l:.2f}  Lot-1 Target={t_l:.2f}")
                if short_state == "PENDING" and e_s and l <= e_s:
                    short_state = "ACTIVE_P1"; short_entry = e_s; short_entry_date = day_str
                    ev(day_str, t, f"🔴 SHORT ENTRY (2 Lots) @ {e_s:.2f}  |  SL1={sl1_s:.2f}  Lot-1 Target={t_s:.2f}")

            # ── Long Phase 1: SL1 or Lot-1 Target ────────────────────────
            if long_state == "ACTIVE_P1":
                if l <= sl1_l:
                    exit_price = min(l, sl1_l)
                    pts = exit_price - long_entry
                    long_pnl = round(pts * LOTS * mult, 2)
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
                    long_pnl = round((long_lot1_pnl or 0) + lot2_pnl, 2)
                    long_exit = exit_price; long_reason = "SL2_HIT"; long_exit_date = day_str
                    long_state = "CLOSED"
                    ev(day_str, t, f"🏁 LONG LOT-2 SL2 HIT @ {exit_price:.2f}  |  Lot-2 closed  |  Lot-2 PnL=₹{lot2_pnl:+.2f}  |  TOTAL=₹{long_pnl:+.2f}")

            if short_state == "ACTIVE_P1":
                if h >= sl1_s:
                    exit_price = max(h, sl1_s)
                    pts = short_entry - exit_price
                    short_pnl = round(pts * LOTS * mult, 2)
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
                    short_pnl = round((short_lot1_pnl or 0) + lot2_pnl, 2)
                    short_exit = exit_price; short_reason = "SL2_HIT"; short_exit_date = day_str
                    short_state = "CLOSED"
                    ev(day_str, t, f"🏁 SHORT LOT-2 SL2 HIT @ {exit_price:.2f}  |  Lot-2 closed  |  Lot-2 PnL=₹{lot2_pnl:+.2f}  |  TOTAL=₹{short_pnl:+.2f}")

            # Early exit if both closed
            if long_state == "CLOSED" and short_state in ("CLOSED", "GAP", "PENDING"):
                break
            if short_state == "CLOSED" and long_state in ("CLOSED", "GAP", "PENDING"):
                break

        # End of this day's intraday replay
        # Prepend this day to rolling history for the next day's SL2 calculation
        if last_close and intraday:
            day_candle = {
                "date":  day_str,
                "high":  max(c["high"] for c in intraday),
                "low":   min(c["low"]  for c in intraday),
                "open":  intraday[0]["open"],
                "close": last_close,
            }
            rolling_history.insert(0, day_candle)

        # Move to next trading day
        sim_date += timedelta(days=1)
        while sim_date.weekday() >= 5:
            sim_date += timedelta(days=1)

    # ── Unrealized P&L for still-open positions ──────────────────────────
    if last_close:
        if long_state in ("ACTIVE_P1", "ACTIVE_P2") and long_entry:
            live_pts = last_close - long_entry
            lots_rem = LOTS if long_state == "ACTIVE_P1" else 1
            long_pnl = round((long_lot1_pnl or 0) + (live_pts * lots_rem * mult), 2)
        if short_state in ("ACTIVE_P1", "ACTIVE_P2") and short_entry:
            live_pts = short_entry - last_close
            lots_rem = LOTS if short_state == "ACTIVE_P1" else 1
            short_pnl = round((short_lot1_pnl or 0) + (live_pts * lots_rem * mult), 2)

    effective_levels = _snapshot_levels(gl, e_l, e_s, t_l, t_s, sl1_l, sl1_s, sl2_l, sl2_s)

    return {
        "long_state":      long_state,
        "short_state":     short_state,
        "long_entry":      long_entry,
        "short_entry":     short_entry,
        "long_lot1_pnl":   long_lot1_pnl,
        "short_lot1_pnl":  short_lot1_pnl,
        "long_entry_date": long_entry_date,
        "long_exit_date":  long_exit_date,
        "short_entry_date": short_entry_date,
        "short_exit_date":  short_exit_date,
        "long_exit":       long_exit,
        "short_exit":      short_exit,
        "long_reason":     long_reason,
        "short_reason":    short_reason,
        "long_pnl":        long_pnl,
        "short_pnl":       short_pnl,
        "last_price":      last_close,
        "final_sl2_long":  sl2_l,
        "final_sl2_short": sl2_s,
        "events":          events,
        "effective_levels": effective_levels,
        "initial_levels": effective_levels,
    }
