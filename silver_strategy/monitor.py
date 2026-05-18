"""
Gold Strategy State Machine & LTP Monitor
=========================================
States per instrument-direction (LONG / SHORT tracked independently):
  PENDING      — Entry order placed, waiting for price to reach E_L / E_S
  GAP          — Gap detected 9:00-9:10 AM, entry skipped (rules TBD)
  ACTIVE_P1    — Position open, Phase 1 SL active (both lots)
  ACTIVE_P2    — Lot-1 closed at target, Phase 2 SL active for Lot-2 only
  CLOSED       — All lots closed

Gap monitoring window: 9:00 AM – 9:10 AM
SL monitoring: starts at 9:10 AM (time-lock before 9:10 — no SL triggers)
For overnight positions: at next-day 9:10 AM, check & exit if price still beyond SL
"""
import threading
import time
import json
from datetime import datetime, timedelta
from loguru import logger
import pytz
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from .database import Session, get_today_state, get_active_state, upsert_state, get_setting
from .data_fetcher import get_ltp
from .calculator   import SilverLevels, rt
from . import telegram as tg
from core.pnl_logger import log_closed_trade

IST = pytz.timezone("Asia/Kolkata")

INSTRUMENTS = ["SILVER", "SILVERM", "SILVERMIC"]
MULTIPLIERS = {
    "SILVER":    30,
    "SILVERM":   5,
    "SILVERMIC": 1
}
LOTS        = 2  # always 2 lots


# Strategy Branding
STRATEGY_NAME = "SILVER • MathZing"

_live: dict = {inst: {} for inst in INSTRUMENTS}
_lock = threading.Lock()
_first_tick: dict = {inst: True for inst in INSTRUMENTS}  # Track first LTP tick per instrument
_gap_high: dict = {inst: 0.0 for inst in INSTRUMENTS}      # Track high during 9:00-9:10 gap window
_gap_low:  dict = {inst: 999999.0 for inst in INSTRUMENTS} # Track low during 9:00-9:10 gap window
_gap_window_active: dict = {inst: False for inst in INSTRUMENTS}  # True while in 9:00-9:10 window


def _now_ist():
    return datetime.now(IST)


def _time_str() -> str:
    return _now_ist().strftime("%H:%M:%S")


def _is_weekday() -> bool:
    return _now_ist().weekday() < 5


def _recent_completed_weekday(today_str: str | None = None) -> str:
    cursor = _now_ist().date() if today_str is None else datetime.fromisoformat(today_str).date()
    cursor -= timedelta(days=1)
    while cursor.weekday() >= 5: # Skip Sat/Sun
        cursor -= timedelta(days=1)
    return cursor.isoformat()


def _levels_need_refresh(row, today: str) -> bool:
    levels = row.levels if row else {}
    raw_days = levels.get("raw_days", []) if isinstance(levels, dict) else []
    if not raw_days: return True
    latest_date = raw_days[0].get("date")
    if not latest_date: return True
    
    # We need refresh if our latest data date is NOT the most recent completed trading day
    return latest_date != _recent_completed_weekday(today_str=today)


# ─── Load today's state into memory ─────────────────────────────────────────

def load_today_states():
    missing = False
    today = datetime.now(IST).date().isoformat()
    for inst in INSTRUMENTS:
        # Priority 1: Load today's exact state if it exists (preserves CLOSED/PENDING after restart)
        from .database import get_today_state
        today_row = get_today_state(inst)
        if today_row:
            with _lock:
                _live[inst] = _row_to_live(today_row)
            needs_refresh = _levels_need_refresh(today_row, today)
            if needs_refresh:
                logger.info(f"Silver Strategy: Refreshing lookback levels for {inst} on {today}")
                missing = True
        else:
            # Priority 2: Load active overnight trade, else fresh
            row = get_active_state(inst)
            if row:
                with _lock:
                    _live[inst] = _row_to_live(row)
                needs_refresh = _levels_need_refresh(row, today)

                # Upsert to today's ID immediately
                logger.info(f"Silver Strategy: Carrying forward active trade for {inst} from {row.date} to {today}")
                
                # Reset states that are not ACTIVE to PENDING for the new day
                new_long_st = row.long_state if row.long_state in ("ACTIVE_P1", "ACTIVE_P2") else "PENDING"
                new_short_st = row.short_state if row.short_state in ("ACTIVE_P1", "ACTIVE_P2") else "PENDING"

                upsert_state(inst, {
                    "long_state": new_long_st,
                    "long_entry_price": row.long_entry_price if new_long_st != "PENDING" else None,
                    "long_entry_date": row.long_entry_date if new_long_st != "PENDING" else None,
                    "long_lot1_closed": row.long_lot1_closed if new_long_st != "PENDING" else False,
                    "long_pnl": row.long_pnl if new_long_st != "PENDING" else 0,
                    "short_state": new_short_st,
                    "short_entry_price": row.short_entry_price if new_short_st != "PENDING" else None,
                    "short_entry_date": row.short_entry_date if new_short_st != "PENDING" else None,
                    "short_lot1_closed": row.short_lot1_closed if new_short_st != "PENDING" else False,
                    "short_pnl": row.short_pnl if new_short_st != "PENDING" else 0,
                    "auto_trade": row.auto_trade,
                    "h4": row.h4, "l4": row.l4, "h2": row.h2, "l2": row.l2,
                    "levels_json": row.levels_json
                })
                needs_refresh = True
                if needs_refresh:
                    logger.info(f"Silver Strategy: Refreshing lookback levels for {inst} on {today}")
                    missing = True
            else:
                missing = True
            
    if missing:
        logger.info("Silver Strategy: No states found for today. Triggering automatic fetch...")
        from .scheduler import fetch_now
        try:
            fetch_now()
        except Exception as e:
            logger.error(f"Automatic fetch failed: {e}")
    else:
        logger.info("Silver Strategy: today's states loaded/synced from DB")


def sync_live(instrument: str, type: str, sim: dict):
    """Update live state from a backtest simulation (taking over a trade)."""
    with _lock:
        if instrument not in _live:
            return {"success": False, "error": f"Instrument {instrument} not supported"}
            
        state = _live[instrument]
        levels = sim.get("effective_levels") or sim.get("levels") or sim.get("initial_levels", {})
        if levels:
            state["levels"] = levels
        if type == "LONG":
            state["long_state"] = sim.get("long_state", "PENDING")
            state["long_entry_price"] = sim.get("long_entry")
            state["long_lot1_closed"] = (state["long_state"] == "ACTIVE_P2" or state["long_state"] == "CLOSED")
            state["long_pnl"] = sim.get("long_lot1_pnl") or 0
        else:
            state["short_state"] = sim.get("short_state", "PENDING")
            state["short_entry_price"] = sim.get("short_entry")
            state["short_lot1_closed"] = (state["short_state"] == "ACTIVE_P2" or state["short_state"] == "CLOSED")
            state["short_pnl"] = sim.get("short_lot1_pnl") or 0
            
        # Persist to DB including levels from simulation
        upsert_state(instrument, {
            "long_state": state["long_state"],
            "long_entry_price": state["long_entry_price"],
            "long_entry_date": state.get("long_entry_date"),
            "long_lot1_closed": state["long_lot1_closed"],
            "long_pnl": state["long_pnl"],
            "short_state": state["short_state"],
            "short_entry_price": state["short_entry_price"],
            "short_entry_date": state.get("short_entry_date"),
            "short_lot1_closed": state["short_lot1_closed"],
            "short_pnl": state["short_pnl"],
            "levels_json": json.dumps(levels) if levels else state.get("levels_json")
        })
        
    logger.info(f"Silver Strategy: {instrument} {type} synced from backtest to live")
    return {"success": True}


def _row_to_live(row) -> dict:
    lvl = row.levels
    raw_days = lvl.get("raw_days", [])
    initial_ltp = raw_days[0].get("close") if raw_days else None

    d = {
        "instrument":       row.instrument,
        "trading_symbol":   row.trading_symbol,
        "token":            row.token,
        "lot_size":         row.lot_size,
        "h4": row.h4, "l4": row.l4, "h2": row.h2, "l2": row.l2,
        "levels":           lvl,
        "long_state":       row.long_state,
        "short_state":      row.short_state,
        "long_entry_price": row.long_entry_price,
        "long_lot1_closed": row.long_lot1_closed,
        "long_exit_price":  row.long_exit_price,
        "long_exit_reason": row.long_exit_reason,
        "long_pnl":         row.long_pnl or 0,
        "long_entry_date":  row.long_entry_date,
        "short_entry_price":row.short_entry_price,
        "short_lot1_closed":row.short_lot1_closed,
        "short_entry_date": row.short_entry_date,
        "short_exit_price": row.short_exit_price,
        "short_exit_reason":row.short_exit_reason,
        "short_pnl":        row.short_pnl or 0,
        "auto_trade":       row.auto_trade,
        "ltp":              initial_ltp,
        "last_update":      None,
    }
    _recalculate_active_levels(d)  # Apply entry-based overrides if active
    return d

def rt(val):
    """Round to nearest 0.05 for MCX."""
    return round(round(val / 0.05) * 0.05, 2)

def _recalculate_active_levels(state: dict):
    """Override levels if a trade is active, basing Target/SL on actual entry price.
    
    ACTIVE_P1: fully recalculate sl1 from entry price + b-component.
    ACTIVE_P2: recalculate sl2 'a' from entry price, but PRESERVE the trailing
               ratchet 'sl' value — never move sl2 in the unfavorable direction.
    """
    lvl = state.get("levels", {})
    if not lvl: return

    long_st  = state.get("long_state")
    short_st = state.get("short_state")

    # LONG
    if long_st in ("ACTIVE_P1", "ACTIVE_P2"):
        ep = state.get("long_entry_price")
        if ep:
            lvl["t_l"] = rt(ep * 1.02)
            if "sl1_long" in lvl:
                lvl["sl1_long"]["a"] = rt(ep * 0.98)
                lvl["sl1_long"]["sl"] = max(lvl["sl1_long"]["a"], lvl["sl1_long"].get("b", 0))
            if "sl2_long" in lvl:
                lvl["sl2_long"]["a"] = rt(ep * 0.98)  # Phase 2 SL: entry - 2% (NOT cost itself)
                fresh_sl = max(lvl["sl2_long"]["a"], lvl["sl2_long"].get("b", 0))
                if long_st == "ACTIVE_P2":
                    # Ratchet: sl2 can only move UP (more favorable for long)
                    stored_sl = lvl["sl2_long"].get("sl", 0)
                    lvl["sl2_long"]["sl"] = max(fresh_sl, stored_sl)
                else:
                    lvl["sl2_long"]["sl"] = fresh_sl

    # SHORT
    if short_st in ("ACTIVE_P1", "ACTIVE_P2"):
        ep = state.get("short_entry_price")
        if ep:
            lvl["t_s"] = rt(ep * 0.98)
            if "sl1_short" in lvl:
                # SL1: tie 'a' to ACTUAL entry (protects Lot-1 from >2% loss)
                lvl["sl1_short"]["a"] = rt(ep * 1.02)
                lvl["sl1_short"]["sl"] = min(lvl["sl1_short"]["a"], lvl["sl1_short"].get("b", 9999999))
            if "sl2_short" in lvl:
                lvl["sl2_short"]["a"] = rt(ep * 1.02)  # Phase 2 SL: entry + 2% (NOT cost itself)
                fresh_sl = min(lvl["sl2_short"]["a"], lvl["sl2_short"].get("b", 9999999))
                if short_st == "ACTIVE_P2":
                    # Ratchet: sl2 can only move DOWN (more favorable for short)
                    stored_sl = lvl["sl2_short"].get("sl", 9999999)
                    lvl["sl2_short"]["sl"] = min(fresh_sl, stored_sl)
                else:
                    lvl["sl2_short"]["sl"] = fresh_sl


def set_levels_from_silver_levels(inst: str, gl: SilverLevels):
    """Called after data fetch to push calculated levels into live state and DB."""
    d = gl.to_dict()
    
    # Load defaults from settings if they exist
    import json
    saved_auto = get_setting(f"auto_trade_{inst}", "false") == "true"
    saved_levels_json = get_setting(f"default_levels_{inst}", "")
    if saved_levels_json:
        try:
            saved_levels = json.loads(saved_levels_json)
            for k in ["e_l", "t_l", "e_s", "t_s"]:
                if k in saved_levels: d[k] = saved_levels[k]
            for side in ["long", "short"]:
                for sl_num in ["sl1", "sl2"]:
                    key = f"{sl_num}_{side}"
                    if key in saved_levels: d[key] = saved_levels[key]
            logger.info(f"Silver Strategy: Applied saved default levels for {inst}")
        except Exception as e:
            logger.error(f"Error loading default levels for {inst}: {e}")

    with _lock:
        prev = _live.get(inst, {})
        long_active  = prev.get("long_state")  in ("ACTIVE_P1", "ACTIVE_P2")
        short_active = prev.get("short_state") in ("ACTIVE_P1", "ACTIVE_P2")
        short_active = prev.get("short_state") in ("ACTIVE_P1", "ACTIVE_P2")
        prev_lvl = prev.get("levels", {})

        # ── GAP RECOVERY PRESERVATION: If we restart on the same day, preserve the recovered gap levels
        if prev_lvl.get("long_gap_recovered"):
            d["e_l"] = prev_lvl.get("e_l", d["e_l"])
            d["t_l"] = prev_lvl.get("t_l", d["t_l"])
            if "sl1_long" in prev_lvl: d["sl1_long"] = prev_lvl["sl1_long"]
            if "sl2_long" in prev_lvl: d["sl2_long"] = prev_lvl["sl2_long"]
            d["long_gap_recovered"] = True
            
        if prev_lvl.get("short_gap_recovered"):
            d["e_s"] = prev_lvl.get("e_s", d["e_s"])
            d["t_s"] = prev_lvl.get("t_s", d["t_s"])
            if "sl1_short" in prev_lvl: d["sl1_short"] = prev_lvl["sl1_short"]
            if "sl2_short" in prev_lvl: d["sl2_short"] = prev_lvl["sl2_short"]
            d["short_gap_recovered"] = True

        # ── H4/L4 LOCK: If a trade is active, freeze the entry-time H4/L4.
        # Only H2/L2 (2-day) is allowed to trail for SL1 purposes.
        # SL2 B-point uses locked H4/L4 — it MUST NOT change after entry.
        if long_active or short_active:
            locked_h4 = prev.get("h4") or gl.h4
            locked_l4 = prev.get("l4") or gl.l4
            fresh_h2  = gl.h2   # 2-day high trails daily → used for SL1 'b'
            fresh_l2  = gl.l2
            logger.info(f"{inst}: Trade ACTIVE — H4/L4 locked ({locked_h4}/{locked_l4}), "
                        f"H2/L2 trail updated to H2={fresh_h2}/L2={fresh_l2}")
            # Update SL1 trailing B-component with fresh 2-day data
            if short_active and "sl1_short" in d:
                d["sl1_short"]["b"] = rt(fresh_h2 * 1.0012)
                d["sl1_short"]["sl"] = min(d["sl1_short"]["a"],
                                           d["sl1_short"]["b"])
            # SL2 B-point stays locked to entry-time H4
            if short_active and "sl2_short" in d:
                d["sl2_short"]["b"] = rt(locked_h4 * 1.0012)
                d["sl2_short"]["sl"] = min(d["sl2_short"]["a"],
                                           d["sl2_short"]["b"])
            if long_active and "sl1_long" in d:
                d["sl1_long"]["b"] = rt(fresh_l2 * 0.9988)
                d["sl1_long"]["sl"] = max(d["sl1_long"]["a"],
                                          d["sl1_long"]["b"])
            # SL2 B-point stays locked to entry-time L4
            if long_active and "sl2_long" in d:
                d["sl2_long"]["b"] = rt(locked_l4 * 0.9988)
                d["sl2_long"]["sl"] = max(d["sl2_long"]["a"],
                                          d["sl2_long"]["b"])
        else:
            locked_h4 = gl.h4
            locked_l4 = gl.l4
            fresh_h2  = gl.h2
            fresh_l2  = gl.l2

        old_lvl  = prev.get("levels", {})
        long_st  = prev.get("long_state")
        short_st = prev.get("short_state")

        # Never move SL in the wrong direction (ratchet protection)
        if long_st in ("ACTIVE_P1", "ACTIVE_P2"):
            if "sl1_long" in d and "sl1_long" in old_lvl:
                d["sl1_long"]["sl"] = max(d["sl1_long"]["sl"], old_lvl["sl1_long"]["sl"])
                d["sl1_long"]["b"] = max(d["sl1_long"]["b"], old_lvl["sl1_long"].get("b", 0))
            if "sl2_long" in d and "sl2_long" in old_lvl:
                d["sl2_long"]["sl"] = max(d["sl2_long"]["sl"], old_lvl["sl2_long"]["sl"])
                d["sl2_long"]["b"] = max(d["sl2_long"]["b"], old_lvl["sl2_long"].get("b", 0))
                
        if short_st in ("ACTIVE_P1", "ACTIVE_P2"):
            if "sl1_short" in d and "sl1_short" in old_lvl:
                d["sl1_short"]["sl"] = min(d["sl1_short"]["sl"], old_lvl["sl1_short"]["sl"])
                d["sl1_short"]["b"] = min(d["sl1_short"]["b"], old_lvl["sl1_short"].get("b", 9999999))
            if "sl2_short" in d and "sl2_short" in old_lvl:
                d["sl2_short"]["sl"] = min(d["sl2_short"]["sl"], old_lvl["sl2_short"]["sl"])
                d["sl2_short"]["b"] = min(d["sl2_short"]["b"], old_lvl["sl2_short"].get("b", 9999999))

        # Use locked H4/L4 for active trades, fresh values otherwise
        use_h4 = locked_h4 if (long_active or short_active) else gl.h4
        use_l4 = locked_l4 if (long_active or short_active) else gl.l4

        _live[inst] = {
            "instrument":       gl.instrument,
            "trading_symbol":   gl.trading_symbol,
            "token":            gl.token,
            "lot_size":         1,   # qty per lot handled at order level
            "h4": use_h4, "l4": use_l4, "h2": gl.h2, "l2": gl.l2,
            "levels":           d,
            "long_state":       prev.get("long_state", "PENDING"),
            "long_entry_price": prev.get("long_entry_price"),
            "long_entry_date":  prev.get("long_entry_date"),
            "long_lot1_closed": prev.get("long_lot1_closed", False),
            "long_exit_price":  prev.get("long_exit_price"),
            "long_exit_reason": prev.get("long_exit_reason"),
            "long_pnl":         prev.get("long_pnl", 0),
            "short_state":      prev.get("short_state", "PENDING"),
            "short_entry_price":prev.get("short_entry_price"),
            "short_entry_date": prev.get("short_entry_date"),
            "short_lot1_closed":prev.get("short_lot1_closed", False),
            "short_exit_price": prev.get("short_exit_price"),
            "short_exit_reason":prev.get("short_exit_reason"),
            "short_pnl":        prev.get("short_pnl", 0),
            "auto_trade":       saved_auto,
            "ltp":              gl.raw_days[0].get("close") if gl.raw_days else None,
            "last_update":      prev.get("last_update"),
        }
        _recalculate_active_levels(_live[inst])  # Apply entry-price overrides
    
    # On every fresh daily fetch — clear gap_recovered flags so next day is a clean slate
    # Fresh 4-day values from gl.to_dict() always replace any previous gap recovery values
    upsert_state(inst, {
        "trading_symbol":   gl.trading_symbol,
        "token":            gl.token,
        "h4": use_h4, "l4": use_l4, "h2": gl.h2, "l2": gl.l2,
        "levels_json":      json.dumps(d),
        "fetched_at":       _now_ist().isoformat(),
        "long_state":       _live[inst]["long_state"],
        "long_entry_price": _live[inst]["long_entry_price"],
        "long_entry_date":  _live[inst]["long_entry_date"],
        "long_lot1_closed": _live[inst]["long_lot1_closed"],
        "long_pnl":         _live[inst]["long_pnl"],
        "long_gap_recovered": False,
        "short_state":      _live[inst]["short_state"],
        "short_entry_price":_live[inst]["short_entry_price"],
        "short_entry_date": _live[inst]["short_entry_date"],
        "short_lot1_closed":_live[inst]["short_lot1_closed"],
        "short_pnl":        _live[inst]["short_pnl"],
        "short_gap_recovered": False,
        "auto_trade":       saved_auto,
    })


def get_live_state(inst: str) -> dict:
    with _lock:
        return dict(_live.get(inst, {}))


def get_all_live() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _live.items()}


# ─── Main monitor tick (called every 5 seconds) ──────────────────────────────

def _monitor_tick():
    if not _is_weekday():
        return
    ts = _time_str()   # HH:MM:SS

    for inst in INSTRUMENTS:
        with _lock:
            state = _live.get(inst, {})
        if not state or not state.get("token"):
            continue

        lvl   = state.get("levels", {})
        # Always use e_l/e_s directly — they reflect the current working calculation
        # (fresh 4-day on normal day, gap recovery values after 9:15 AM on gap day)
        e_l   = lvl.get("e_l", 0)
        e_s   = lvl.get("e_s", 0)
        t_l   = lvl.get("t_l", 0)
        t_s   = lvl.get("t_s", 0)
        sl1l  = lvl.get("sl1_long",  {}).get("sl", 0)
        sl1s  = lvl.get("sl1_short", {}).get("sl", 0)
        sl2l  = lvl.get("sl2_long",  {}).get("sl", 0)
        sl2s  = lvl.get("sl2_short", {}).get("sl", 0)

        ltp = get_ltp(state["token"], state["trading_symbol"])
        if ltp is None:
            continue

        with _lock:
            _live[inst]["ltp"]         = ltp
            _live[inst]["last_update"] = _now_ist().isoformat()

        long_st  = state["long_state"]
        short_st = state["short_state"]
        morning_processed = state.get("morning_processed", False)

        # ── Set Morning Processed Flag: Just after 09:10 AM ──────────
        if ts == "09:10:05" or ("09:10:05" <= ts <= "09:10:15"):
            if not morning_processed:
                _set_state(inst, "morning_processed", True)
                logger.info(f"{inst}: Morning session marked as PROCESSED.")

        # ── POST-STARTUP GAP CHECK (runs only on first LTP tick after app restart) ──
        if _first_tick.get(inst, True):
            _first_tick[inst] = False
            if ts > "09:10:00" and ts < "17:00:00":  # Only during market hours
                if long_st == "PENDING" and e_l and ltp > e_l:
                    logger.warning(
                        f"{inst}: POST-STARTUP GAP detected. LTP={ltp} already ABOVE E_L={e_l}. "
                        "Marking as GAP to prevent wrong entry after restart."
                    )
                    _set_state(inst, "long_state", "GAP")
                    long_st = "GAP"
                    tg.send_msg(
                        f"⚡ *{inst} Startup GAP Detected*\n"
                        f"App restarted during market hours.\n"
                        f"LTP *{ltp}* already above E_L *{e_l}*.\n"
                        f"Marked as GAP — manual review needed."
                    )
                if short_st == "PENDING" and e_s and ltp < e_s:
                    logger.warning(
                        f"{inst}: POST-STARTUP GAP detected. LTP={ltp} already BELOW E_S={e_s}. "
                        "Marking as GAP to prevent wrong entry after restart."
                    )
                    _set_state(inst, "short_state", "GAP")
                    short_st = "GAP"
                    tg.send_msg(
                        f"⚡ *{inst} Startup GAP Detected*\n"
                        f"App restarted during market hours.\n"
                        f"LTP *{ltp}* already below E_S *{e_s}*.\n"
                        f"Marked as GAP — manual review needed."
                    )

        # ── Gap Monitoring: 9:00 – 9:10 AM ───────────────────────────
        if "09:00:00" <= ts < "09:10:00":
            # Track high/low during gap window for use in 9:15 recovery
            _gap_high[inst] = max(_gap_high[inst], ltp)
            _gap_low[inst]  = min(_gap_low[inst],  ltp)
            _gap_window_active[inst] = True

            if long_st == "PENDING" and e_l and ltp >= e_l:
                _set_state(inst, "long_state", "GAP")
                logger.warning(f"{inst}: GAP UP detected at {ltp} (E_L={e_l})")
                tg.send_gap(inst, "LONG", ltp, e_l)
            if short_st == "PENDING" and e_s and ltp <= e_s:
                _set_state(inst, "short_state", "GAP")
                logger.warning(f"{inst}: GAP DOWN at {ltp} (E_S={e_s})")
                tg.send_gap(inst, "SHORT", ltp, e_s)
            
            # --- Rule: HOLDING TARGETS active at 09:00 AM ---
            if long_st == "ACTIVE_P1" and t_l and ltp >= t_l:
                _set_state(inst, "long_lot1_closed", True)
                _set_state(inst, "long_state", "ACTIVE_P2")
                mult = MULTIPLIERS.get(inst, 1)
                lot1_pnl = round((t_l - (state.get("long_entry_price") or t_l)) * 1 * mult, 2)
                _set_state(inst, "long_pnl", lot1_pnl)
                tg.send_lot1_target_hit(inst, "LONG", t_l, lot1_pnl, sl2l)
            elif long_st == "ACTIVE_P2" and sl2l and ltp <= sl2l:
                _close_long(inst, ltp, "SL2_HIT", sl2l)

            if short_st == "ACTIVE_P1" and t_s and ltp <= t_s:
                _set_state(inst, "short_lot1_closed", True)
                _set_state(inst, "short_state", "ACTIVE_P2")
                mult = MULTIPLIERS.get(inst, 1)
                lot1_pnl = round(((state.get("short_entry_price") or t_s) - t_s) * 1 * mult, 2)
                _set_state(inst, "short_pnl", lot1_pnl)
                tg.send_lot1_target_hit(inst, "SHORT", t_s, lot1_pnl, sl2s)
            elif short_st == "ACTIVE_P2" and sl2s and ltp >= sl2s:
                _close_short(inst, ltp, "SL2_HIT", sl2s)

            continue  # No NEW Entries or NORMAL SL checks during gap window

        # ── Evening Gap Monitoring: 17:00 – 17:10 PM ────────────────
        # Rule: Only if morning session was NOT processed (e.g. Morning Holiday)
        if "17:00:00" <= ts < "17:10:00" and not morning_processed:
            if long_st == "PENDING" and e_l and ltp >= e_l:
                _set_state(inst, "long_state", "GAP")
                logger.warning(f"{inst}: EVENING GAP UP detected at {ltp} (E_L={e_l})")
                tg.send_gap(inst, "LONG", ltp, e_l)
            if short_st == "PENDING" and e_s and ltp <= e_s:
                _set_state(inst, "short_state", "GAP")
                logger.warning(f"{inst}: EVENING GAP DOWN at {ltp} (E_S={e_s})")
                tg.send_gap(inst, "SHORT", ltp, e_s)
            continue

        # ── After 9:10 AM: Entry triggers & SL/Target monitoring ─────
        if ts < "09:10:00":
            continue  # Entry/SL Lock: no action before 9:10

        # Special 9:15 AM SL Reset Rule for Active Trades
        if ts == "09:15:00" or ("09:15:00" <= ts <= "09:15:10"):
            _handle_915_sl_reset(inst, state, ltp)

        # Time-lock for SL monitoring: Starts at 9:10 AM (Normal Rule)
        # Note: 9:15 AM Gap Reset will override these if a gap occurred.
        if ts < "09:10:00":
            continue

        # Long entry trigger
        if long_st == "PENDING" and e_l and ltp >= e_l:
            entry_price = e_l
            long_st = "ACTIVE_P1"
            entry_dt = _now_ist().isoformat()
            _set_state(inst, "long_state", "ACTIVE_P1")
            _set_state(inst, "long_entry_price", entry_price)
            _set_state(inst, "long_entry_date", entry_dt)
            _recalculate_active_levels(_live[inst])
            t_l = _live[inst]["levels"]["t_l"]
            sl1l = _live[inst]["levels"]["sl1_long"]["sl"]
            logger.info(f"{inst}: LONG ENTRY triggered at level {entry_price} (ltp={ltp}) | New Target: {t_l}")
            tg.send_entry_triggered(inst, "LONG", entry_price, t_l, sl1l, state["trading_symbol"])
            auto = _live[inst].get("auto_trade", False)
            _send_to_main_app(inst, "BUY", e_l, sl1l, [t_l], state, auto, STRATEGY_NAME)

        # Short entry trigger
        if short_st == "PENDING" and e_s and ltp <= e_s:
            entry_price = e_s
            short_st = "ACTIVE_P1"
            entry_dt = _now_ist().isoformat()
            _set_state(inst, "short_state", "ACTIVE_P1")
            _set_state(inst, "short_entry_price", entry_price)
            _set_state(inst, "short_entry_date", entry_dt)
            _recalculate_active_levels(_live[inst])
            t_s = _live[inst]["levels"]["t_s"]
            sl1s = _live[inst]["levels"]["sl1_short"]["sl"]
            logger.info(f"{inst}: SHORT ENTRY triggered at level {entry_price} (ltp={ltp}) | New Target: {t_s}")
            tg.send_entry_triggered(inst, "SHORT", entry_price, t_s, sl1s, state["trading_symbol"])
            auto = _live[inst].get("auto_trade", False)
            _send_to_main_app(inst, "SELL", e_s, sl1s, [t_s], state, auto, STRATEGY_NAME)

        # Long Phase 1: check SL and Target
        if long_st == "ACTIVE_P1":
            if sl1l and ltp <= sl1l:
                _close_long(inst, ltp, "SL1_HIT", sl1l)
            elif t_l and ltp >= t_l:
                _set_state(inst, "long_lot1_closed", True)
                _set_state(inst, "long_state", "ACTIVE_P2")
                mult = MULTIPLIERS.get(inst, 1)
                lot1_pnl = round((t_l - (_live[inst].get("long_entry_price") or t_l)) * 1 * mult, 2)
                _set_state(inst, "long_pnl", lot1_pnl)
                logger.info(f"{inst}: Long Lot-1 TARGET HIT @ {ltp}. Phase 2 SL={sl2l}")
                tg.send_lot1_target_hit(inst, "LONG", t_l, lot1_pnl, sl2l)
                # Log Lot-1 profit to P&L report immediately
                from core.pnl_logger import log_closed_trade
                log_closed_trade(
                    instrument=inst, trading_symbol=_live[inst].get("trading_symbol", inst),
                    direction="LONG", entry_price=(_live[inst].get("long_entry_price") or t_l),
                    exit_price=t_l, entry_date=_live[inst].get("long_entry_date", ""),
                    exit_reason="PARTIAL_TARGET", lots=1, lot_size=mult, strategy=STRATEGY_NAME
                )

        # Long Phase 2: check SL only (Lot-2)
        if long_st == "ACTIVE_P2" or _live[inst].get("long_state") == "ACTIVE_P2":
            if sl2l and ltp <= sl2l:
                _close_long(inst, ltp, "SL2_HIT", sl2l)

        # Short Phase 1: check SL and Target
        if short_st == "ACTIVE_P1":
            if sl1s and ltp >= sl1s:
                _close_short(inst, ltp, "SL1_HIT", sl1s)
            elif t_s and ltp <= t_s:
                _set_state(inst, "short_lot1_closed", True)
                _set_state(inst, "short_state", "ACTIVE_P2")
                mult = MULTIPLIERS.get(inst, 1)
                lot1_pnl = round(((_live[inst].get("short_entry_price") or t_s) - t_s) * 1 * mult, 2)
                _set_state(inst, "short_pnl", lot1_pnl)
                logger.info(f"{inst}: Short Lot-1 TARGET HIT @ {ltp}. Phase 2 SL={sl2s}")
                tg.send_lot1_target_hit(inst, "SHORT", t_s, lot1_pnl, sl2s)
                # Log Lot-1 profit to P&L report immediately
                from core.pnl_logger import log_closed_trade
                log_closed_trade(
                    instrument=inst, trading_symbol=_live[inst].get("trading_symbol", inst),
                    direction="SHORT", entry_price=(_live[inst].get("short_entry_price") or t_s),
                    exit_price=t_s, entry_date=_live[inst].get("short_entry_date", ""),
                    exit_reason="PARTIAL_TARGET", lots=1, lot_size=mult, strategy=STRATEGY_NAME
                )

        # Short Phase 2:
        if short_st == "ACTIVE_P2" or _live[inst].get("short_state") == "ACTIVE_P2":
            if sl2s and ltp >= sl2s:
                _close_short(inst, ltp, "SL2_HIT", sl2s)


def _set_state(inst: str, key: str, val):
    with _lock:
        _live[inst][key] = val
    upsert_state(inst, {key: val})

def _handle_915_sl_reset(inst: str, state: dict, ltp: float):
    """
    Rule at 9:15 AM:
    1. If long/short state is ACTIVE and SL was breached during gap → Reset SL
    2. If long/short state is GAP → GAP RECOVERY: Calculate new entry from 15-min high/low
       (Matches backtester: new_e_l = gap_high × 1.0012, reset to PENDING with new levels)
    """
    today = _now_ist().date().isoformat()
    last_reset = state.get("last_sl_reset_date")
    if last_reset == today:
        return

    lvl = state.get("levels", {})
    changed = False
    
    # Fetch the 09:00-09:15 candle range from Angel One
    try:
        from data.angel_api import angel_api
        now = _now_ist()
        from_t = now.strftime("%Y-%m-%d 09:00")
        to_t   = now.strftime("%Y-%m-%d 09:15")
        candles = angel_api.get_candle_data(state["token"], "MCX", "FIVE_MINUTE", from_t, to_t)
        
        # Fallback: Use in-memory tracked values if candle fetch fails
        if candles:
            m15_high = max(float(c[2]) for c in candles)
            m15_low  = min(float(c[3]) for c in candles)
        else:
            m15_high = _gap_high.get(inst, 0)
            m15_low  = _gap_low.get(inst, 999999)
            if m15_high == 0 or m15_low == 999999:
                logger.warning(f"{inst}: Cannot do 9:15 reset — no candles and no tracked gap range")
                return
            logger.info(f"{inst}: Using in-memory gap range: high={m15_high}, low={m15_low}")

        from .calculator import rt

        # ── CASE 1: GAP RECOVERY ──────────────────────────────────────────────────
        # Gap recovery updates e_l/e_s so dashboard reflects new calculation.
        # Next day's fresh fetch always overwrites with fresh 4-day values.
        if state["long_state"] == "GAP":
            new_e_l  = rt(m15_high * 1.0012)
            new_t_l  = rt(new_e_l * 1.015)
            sl1_a    = rt(new_e_l * 0.985)
            sl1_b    = lvl.get("sl1_long", {}).get("b", 0)
            new_sl1_l = max(sl1_a, sl1_b)
            sl2_a    = rt(new_e_l * 0.985)
            sl2_b    = lvl.get("sl2_long", {}).get("b", 0)
            new_sl2_l = max(sl2_a, sl2_b)

            # Update e_l/t_l so dashboard reflects the new gap recovery calculation
            lvl["e_l"] = new_e_l
            lvl["t_l"] = new_t_l
            if "sl1_long" in lvl:
                lvl["sl1_long"]["a"] = sl1_a
                lvl["sl1_long"]["sl"] = new_sl1_l
            if "sl2_long" in lvl:
                lvl["sl2_long"]["a"] = sl2_a
                lvl["sl2_long"]["sl"] = new_sl2_l
                
            lvl["long_gap_recovered"] = True

            _set_state(inst, "levels", lvl)
            _set_state(inst, "long_state", "PENDING")
            _set_state(inst, "long_gap_recovered", True)
            changed = True
            logger.info(
                f"{inst}: GAP RECOVERY (LONG) at 9:15 — "
                f"New E_L={new_e_l} | T_L={new_t_l} | SL1={new_sl1_l} "
                f"(15-min high={m15_high})"
            )
            tg.send_msg(
                f"📊 *{inst} GAP RECOVERY at 9:15 AM*\n"
                f"15-min High: *{m15_high}*\n"
                f"New Long Entry: *{new_e_l}* (High × 1.0012)\n"
                f"Target: *{new_t_l}* | SL1: *{new_sl1_l}*\n"
                f"Status → PENDING (waiting for entry trigger)"
            )

        if state["short_state"] == "GAP":
            new_e_s  = rt(m15_low * 0.9988)
            new_t_s  = rt(new_e_s * 0.985)
            sl1_a    = rt(new_e_s * 1.015)
            sl1_b    = lvl.get("sl1_short", {}).get("b", 9999999)
            new_sl1_s = min(sl1_a, sl1_b)
            sl2_a    = rt(new_e_s * 1.015)
            sl2_b    = lvl.get("sl2_short", {}).get("b", 9999999)
            new_sl2_s = min(sl2_a, sl2_b)

            # Update e_s/t_s so dashboard reflects the new gap recovery calculation
            lvl["e_s"] = new_e_s
            lvl["t_s"] = new_t_s
            if "sl1_short" in lvl:
                lvl["sl1_short"]["a"] = sl1_a
                lvl["sl1_short"]["sl"] = new_sl1_s
            if "sl2_short" in lvl:
                lvl["sl2_short"]["a"] = sl2_a
                lvl["sl2_short"]["sl"] = new_sl2_s

            lvl["short_gap_recovered"] = True

            _set_state(inst, "levels", lvl)
            _set_state(inst, "short_state", "PENDING")
            _set_state(inst, "short_gap_recovered", True)
            changed = True
            logger.info(
                f"{inst}: GAP RECOVERY (SHORT) at 9:15 — "
                f"New E_S={new_e_s} | T_S={new_t_s} | SL1={new_sl1_s} "
                f"(15-min low={m15_low})"
            )
            tg.send_msg(
                f"📊 *{inst} GAP RECOVERY at 9:15 AM*\n"
                f"15-min Low: *{m15_low}*\n"
                f"New Short Entry: *{new_e_s}* (Low × 0.9988)\n"
                f"Target: *{new_t_s}* | SL1: *{new_sl1_s}*\n"
                f"Status → PENDING (waiting for entry trigger)"
            )

        # ── CASE 2: SL RESET for ACTIVE positions (existing logic) ──────────────────
        if state["short_state"] in ("ACTIVE_P1", "ACTIVE_P2"):
            sl_key = "sl1_short" if state["short_state"] == "ACTIVE_P1" else "sl2_short"
            curr_sl = lvl.get(sl_key, {}).get("sl", 0)
            if curr_sl and ltp >= curr_sl:
                new_sl = rt(m15_high * 1.0012)
                lvl[sl_key]["sl"] = new_sl
                lvl[sl_key]["b"] = new_sl
                changed = True
                logger.info(f"{inst}: SHORT SL Gap Reset at 9:15. New SL: {new_sl} (based on 15m high {m15_high})")
                tg.send_msg(f"🔔 *{inst} SL RESET (9:15 AM)*\nShort SL was breached by gap. New Trailing SL set at *{new_sl}* (High + 0.12%)")

        if state["long_state"] in ("ACTIVE_P1", "ACTIVE_P2"):
            sl_key = "sl1_long" if state["long_state"] == "ACTIVE_P1" else "sl2_long"
            curr_sl = lvl.get(sl_key, {}).get("sl", 0)
            if curr_sl and ltp <= curr_sl:
                new_sl = rt(m15_low * 0.9988)
                lvl[sl_key]["sl"] = new_sl
                lvl[sl_key]["b"] = new_sl
                changed = True
                logger.info(f"{inst}: LONG SL Gap Reset at 9:15. New SL: {new_sl} (based on 15m low {m15_low})")
                tg.send_msg(f"🔔 *{inst} SL RESET (9:15 AM)*\nLong SL was breached by gap. New Trailing SL set at *{new_sl}* (Low - 0.12%)")

        if changed:
            import json
            _set_state(inst, "levels", lvl)
            _set_state(inst, "last_sl_reset_date", today)
            upsert_state(inst, {"levels_json": json.dumps(lvl)})

            # Reset gap tracking for this instrument
            _gap_high[inst] = 0.0
            _gap_low[inst]  = 999999.0
            _gap_window_active[inst] = False
            
    except Exception as e:
        logger.error(f"Error in 9:15 SL reset for {inst}: {e}")


def _close_long(inst: str, ltp: float, reason: str, sl: float):
    entry    = _live[inst].get("long_entry_price") or sl
    lot1done = _live[inst].get("long_lot1_closed", False)
    lots     = 1 if lot1done else LOTS
    mult     = MULTIPLIERS.get(inst, 1)
    realized_lot1 = _live[inst].get("long_pnl", 0) if lot1done else 0
    pnl = round((ltp - entry) * lots * mult, 2)
    if lot1done:
        pnl = round(realized_lot1 + pnl, 2)
    _set_state(inst, "long_state",        "CLOSED")
    _set_state(inst, "long_exit_price",   ltp)
    _set_state(inst, "long_exit_reason",  reason)
    _set_state(inst, "long_pnl",          pnl)
    logger.info(f"{inst}: LONG CLOSED | {reason} @ {ltp} | PnL={pnl}")
    lots_label = "Lot-2 only" if lot1done else "both lots"
    tg.send_sl_hit(inst, "LONG", sl, entry, pnl, lots_label)
    log_closed_trade(
        instrument=inst,
        trading_symbol=_live[inst].get("trading_symbol", inst),
        direction="LONG",
        entry_price=entry,
        exit_price=ltp,
        entry_date=_live[inst].get("long_entry_date", ""),
        exit_reason=reason,
        lots=lots,
        lot_size=mult,
        strategy=STRATEGY_NAME,
        realized_lot1_pnl=0, # Already logged separately
    )


def _close_short(inst: str, ltp: float, reason: str, sl: float):
    entry    = _live[inst].get("short_entry_price") or sl
    lot1done = _live[inst].get("short_lot1_closed", False)
    lots     = 1 if lot1done else LOTS
    mult     = MULTIPLIERS.get(inst, 1)
    realized_lot1 = _live[inst].get("short_pnl", 0) if lot1done else 0
    pnl = round((entry - ltp) * lots * mult, 2)
    if lot1done:
        pnl = round(realized_lot1 + pnl, 2)
    _set_state(inst, "short_state",        "CLOSED")
    _set_state(inst, "short_exit_price",   ltp)
    _set_state(inst, "short_exit_reason",  reason)
    _set_state(inst, "short_pnl",          pnl)
    logger.info(f"{inst}: SHORT CLOSED | {reason} @ {ltp} | PnL={pnl}")
    lots_label = "Lot-2 only" if lot1done else "both lots"
    tg.send_sl_hit(inst, "SHORT", sl, entry, pnl, lots_label)
    log_closed_trade(
        instrument=inst,
        trading_symbol=_live[inst].get("trading_symbol", inst),
        direction="SHORT",
        entry_price=entry,
        exit_price=ltp,
        entry_date=_live[inst].get("short_entry_date", ""),
        exit_reason=reason,
        lots=lots,
        lot_size=mult,
        strategy=STRATEGY_NAME,
        realized_lot1_pnl=0, # Already logged separately
    )


def _send_to_main_app(inst: str, action: str, price: float, sl: float,
                      targets: list, state: dict, auto: bool, strategy_type: str = "Silver"):
    """Send trade to paper trading engine directly."""
    if not auto:
        logger.info(f"Auto-trade OFF for {inst} — manual approval required")
        return
    try:
        from core.engine import engine
        payload = {
            "symbol":          state["trading_symbol"],
            "exchange":        "MCX",
            "instrument_type": "FUTCOM",
            "action":          action,
            "entry_price":     price,
            "stop_loss":       sl,
            "targets":         targets,
            "quantity":        LOTS * MULTIPLIERS.get(inst, 1),
            "lot_size":        1,
            "trade_type":      "POSITIONAL",
            "strategy":        f"{strategy_type}-Strategy-{inst}",
        }
        trade = engine.add_trade(
            payload,
            lot_size=payload["lot_size"],
            strategy=payload["strategy"]
        )
        if trade:
            logger.info(f"Trade added to main app: {trade.id}")
        else:
            logger.error("Failed to add trade to main app")
    except Exception as e:
        logger.error(f"Failed to send trade to main app: {e}")


# ─── Background thread ───────────────────────────────────────────────────────

_thread = None


def start_monitor():
    global _thread
    if _thread and _thread.is_alive():
        return

    def _loop():
        logger.info("Silver Strategy monitor started")
        while True:
            try:
                _monitor_tick()
            except Exception as e:
                logger.error(f"Silver Strategy monitor error: {e}")
            time.sleep(10)

    _thread = threading.Thread(target=_loop, daemon=True, name="SilverMonitor")
    _thread.start()
