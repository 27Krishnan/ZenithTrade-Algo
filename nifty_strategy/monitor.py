"""
Nifty Strategy State Machine & LTP Monitor
=========================================
States per instrument-direction:
  PENDING      — Waiting for entry (Normal or Gap-recalculated)
  GAP          — Gap detected at 9:15, waiting for 9:30 to recalculate entry
  GAP_RECOVERY — Holding position hit SL at open, waiting for 9:30 to recalculate SL
  ACTIVE_P1    — Position open, Phase 1 SL active
  ACTIVE_P2    — Lot-1 closed at target, Phase 2 SL active
  CLOSED       — All lots closed
"""
import threading
import time
from datetime import datetime, timedelta
import json
from loguru import logger
import pytz
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from .database import Session, get_today_state, get_active_state, upsert_state, get_setting
from .data_fetcher import get_ltp
from .calculator   import NiftyLevels, rt
from . import telegram as tg
from core.pnl_logger import log_closed_trade

IST = pytz.timezone("Asia/Kolkata")

INSTRUMENTS = ["NIFTY"]
MULTIPLIERS = {
    "NIFTY":  65
}
LOTS        = 2

# Strategy Branding
STRATEGY_NAME = "NIFTY • MathZing"

# In-memory live state
_live: dict = {inst: {} for inst in INSTRUMENTS}
# Window tracking for Gap (9:15 to 9:30)
_gap_window: dict = {inst: {"high": 0.0, "low": 999999.0} for inst in INSTRUMENTS}
_lock = threading.Lock()
_first_tick: dict = {inst: True for inst in INSTRUMENTS}

def _now_ist():
    return datetime.now(IST)

def _time_str() -> str:
    return _now_ist().strftime("%H:%M:%S")

def _is_weekday() -> bool:
    return _now_ist().weekday() < 5

from .database import Session, get_today_state, get_active_state, upsert_state, get_setting


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

def load_today_states():
    missing = False
    today = _now_ist().date().isoformat()
    for inst in INSTRUMENTS:
        # Priority: Load active overnight trade, else load today's fresh row
        row = get_active_state(inst)
        if row:
            with _lock:
                _live[inst] = _row_to_live(row)
            needs_refresh = _levels_need_refresh(row, today)

            # If the row we found is NOT from today, upsert it to today's ID immediately
            if row.date != today:
                logger.info(f"Nifty Strategy: Carrying forward active trade for {inst} from {row.date} to {today}")
                
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
                logger.info(f"Nifty Strategy: Refreshing lookback levels for {inst} on {today}")
                missing = True
        else:
            missing = True
    if missing:
        logger.info("Nifty Strategy: Fetching levels...")
        from .scheduler import fetch_now
        try: fetch_now()
        except Exception as e: logger.error(f"Fetch failed: {e}")
    else:
        logger.info("Nifty Strategy: Today's states loaded/synced from DB")


def sync_live(instrument: str, type: str, sim: dict):
    """Update live state from a backtest simulation (taking over a trade)."""
    with _lock:
        if instrument not in _live:
            return {"success": False, "error": f"Instrument {instrument} not supported"}

        state = _live[instrument]
        if type == "LONG":
            state["long_state"] = sim.get("long_state", "PENDING")
            state["long_entry_price"] = sim.get("long_entry")
            state["long_entry_date"] = sim.get("long_entry_date")
            state["long_lot1_closed"] = state["long_state"] in ("ACTIVE_P2", "CLOSED")
            state["long_pnl"] = sim.get("long_lot1_pnl") or 0
        else:
            state["short_state"] = sim.get("short_state", "PENDING")
            state["short_entry_price"] = sim.get("short_entry")
            state["short_entry_date"] = sim.get("short_entry_date")
            state["short_lot1_closed"] = state["short_state"] in ("ACTIVE_P2", "CLOSED")
            state["short_pnl"] = sim.get("short_lot1_pnl") or 0

        levels = sim.get("effective_levels") or sim.get("levels") or sim.get("initial_levels", {})
        if levels:
            state["levels"] = levels

        upsert_state(instrument, {
            "long_state": state["long_state"],
            "long_entry_price": state.get("long_entry_price"),
            "long_entry_date": state.get("long_entry_date"),
            "long_lot1_closed": state.get("long_lot1_closed", False),
            "long_pnl": state.get("long_pnl", 0),
            "short_state": state["short_state"],
            "short_entry_price": state.get("short_entry_price"),
            "short_entry_date": state.get("short_entry_date"),
            "short_lot1_closed": state.get("short_lot1_closed", False),
            "short_pnl": state.get("short_pnl", 0),
            "levels_json": json.dumps(levels) if levels else state.get("levels_json")
        })

    logger.info(f"Nifty Strategy: {instrument} {type} synced from backtest to live")
    return {"success": True}

def _row_to_live(row) -> dict:
    lvl = row.levels if isinstance(row.levels, dict) else {}
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
        "long_entry_date":  row.long_entry_date,
        "long_lot1_closed": row.long_lot1_closed,
        "long_exit_price":  row.long_exit_price,
        "long_exit_reason": row.long_exit_reason,
        "long_pnl":         row.long_pnl or 0,
        "short_entry_price":row.short_entry_price,
        "short_entry_date": row.short_entry_date,
        "short_lot1_closed":row.short_lot1_closed,
        "short_exit_price": row.short_exit_price,
        "short_exit_reason":row.short_exit_reason,
        "short_pnl":        row.short_pnl or 0,
        "auto_trade":       row.auto_trade,
        "ltp":              0.0,
        "last_update":      None,
    }
    _recalculate_active_levels(d)
    return d

def rt(val):
    """Round to nearest 0.05."""
    return round(round(val / 0.05) * 0.05, 2)

def _recalculate_active_levels(state: dict):
    """Override levels if a trade is active, basing Target/SL on actual entry price (1.25%)."""
    lvl = state.get("levels", {})
    if not lvl: return
    
    # LONG
    if state.get("long_state") in ("ACTIVE_P1", "ACTIVE_P2"):
        ep = state.get("long_entry_price")
        if ep:
            lvl["t_l"] = rt(ep * 1.0125)
            if "sl1_long" in lvl:
                lvl["sl1_long"]["a"] = rt(ep * 0.9875)
                lvl["sl1_long"]["sl"] = max(lvl["sl1_long"]["a"], lvl["sl1_long"].get("b", 0))
            if "sl2_long" in lvl:
                lvl["sl2_long"]["a"] = ep
                lvl["sl2_long"]["sl"] = max(lvl["sl2_long"]["a"], lvl["sl2_long"].get("b", 0))
    
    # SHORT
    if state.get("short_state") in ("ACTIVE_P1", "ACTIVE_P2"):
        ep = state.get("short_entry_price")
        if ep:
            lvl["t_s"] = rt(ep * 0.9875)
            if "sl1_short" in lvl:
                lvl["sl1_short"]["a"] = rt(ep * 1.0125)
                lvl["sl1_short"]["sl"] = min(lvl["sl1_short"]["a"], lvl["sl1_short"].get("b", 9999999))
            if "sl2_short" in lvl:
                lvl["sl2_short"]["a"] = ep
                lvl["sl2_short"]["sl"] = min(lvl["sl2_short"]["a"], lvl["sl2_short"].get("b", 9999999))

def set_levels_from_nifty_levels(inst: str, gl: NiftyLevels):
    d = gl.to_dict()
    
    # Load defaults from settings if they exist
    import json
    saved_auto = get_setting(f"auto_trade_{inst}", "false") == "true"
    saved_levels_json = get_setting(f"default_levels_{inst}", "")
    if saved_levels_json:
        try:
            saved_levels = json.loads(saved_levels_json)
            # Only override if keys match
            for k in ["e_l", "t_l", "e_s", "t_s"]:
                if k in saved_levels:
                    d[k] = saved_levels[k]
            # Handle stoplosses nested
            for side in ["long", "short"]:
                for sl_num in ["sl1", "sl2"]:
                    key = f"{sl_num}_{side}"
                    if key in saved_levels:
                        d[key] = saved_levels[key]
            logger.info(f"Nifty Strategy: Applied saved default levels for {inst}")
        except Exception as e:
            logger.error(f"Error loading default levels for {inst}: {e}")

    # Force refresh from DB to catch manual injections/holding trades
    from .database import get_today_state, get_active_state
    row = get_active_state(inst) or get_today_state(inst)
    if row:
        with _lock:
            if inst not in _live: _live[inst] = {}
            _live[inst].update(_row_to_live(row))
            logger.info(f"Nifty Strategy: Refreshed {inst} state from DB during level update")

    with _lock:
        state = _live[inst]
        old_lvl = state.get("levels", {})
        long_st = state.get("long_state")
        short_st = state.get("short_state")
        
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

        state.update({
            "instrument":       inst,
            "trading_symbol":   gl.trading_symbol,
            "token":            gl.token,
            "h4": gl.h4, "l4": gl.l4, "h2": gl.h2, "l2": gl.l2,
            "levels":           d,
            "long_state":       state.get("long_state", "PENDING"),
            "short_state":      state.get("short_state", "PENDING"),
            "long_entry_price": state.get("long_entry_price"),
            "long_entry_date":  state.get("long_entry_date"),
            "short_entry_price":state.get("short_entry_price"),
            "short_entry_date": state.get("short_entry_date"),
            "long_pnl":         state.get("long_pnl", 0),
            "short_pnl":        state.get("short_pnl", 0),
            "auto_trade":       saved_auto,
        })
    
    # Clear gap_e_l / gap_e_s on every fresh daily fetch — fresh levels take over
    d.pop("gap_e_l", None)
    d.pop("gap_e_s", None)
    d.pop("gap_t_l", None)
    d.pop("gap_t_s", None)

    upsert_state(inst, {
        "trading_symbol":   gl.trading_symbol,
        "token":            gl.token,
        "h4": gl.h4, "l4": gl.l4, "h2": gl.h2, "l2": gl.l2,
        "levels_json":      json.dumps(d),
        "fetched_at":       _now_ist().isoformat(),
        "long_state":       state["long_state"],
        "long_entry_price": state["long_entry_price"],
        "long_entry_date":  state["long_entry_date"],
        "long_pnl":         state["long_pnl"],
        "short_state":      state["short_state"],
        "short_entry_price":state["short_entry_price"],
        "short_entry_date": state["short_entry_date"],
        "short_pnl":        state["short_pnl"],
        "auto_trade":       saved_auto,
    })
    _recalculate_active_levels(_live[inst])

def _monitor_tick():
    ts = _time_str()
    for inst in INSTRUMENTS:
        with _lock:
            state = _live.get(inst, {})
        if not state or not state.get("token"): continue
        
        ltp = get_ltp(state["token"], state["trading_symbol"], exchange="NFO")
        if ltp is None: continue
        
        with _lock:
            _live[inst]["ltp"] = ltp
            _live[inst]["last_update"] = _now_ist().isoformat()

        lvl = state.get("levels", {})
        long_st = state["long_state"]; short_st = state["short_state"]
        # Use gap recovery entry levels if available (set at 9:30 AM after a gap event)
        # Dashboard always shows fresh e_l/e_s; gap_e_l/gap_e_s used only for trade trigger
        eff_e_l = lvl.get("gap_e_l") or lvl.get("e_l", 0)
        eff_e_s = lvl.get("gap_e_s") or lvl.get("e_s", 0)

        # ── POST-STARTUP GAP CHECK (runs only on first LTP tick after app restart) ──
        if _first_tick.get(inst, True):
            _first_tick[inst] = False
            # If app restarts during market hours (after 09:16 normal entry time)
            if ts > "09:16:00" and ts < "15:30:00":
                if long_st == "PENDING" and lvl.get("e_l") and ltp > lvl["e_l"]:
                    logger.warning(
                        f"{inst}: POST-STARTUP GAP detected. LTP={ltp} already ABOVE E_L={lvl['e_l']}. "
                        "Marking as GAP to prevent wrong entry after restart."
                    )
                    _set_state(inst, "long_state", "GAP")
                    long_st = "GAP"
                    tg.send_msg(f"⚡ *{inst} Startup GAP Detected*\nLTP *{ltp}* already above E_L *{lvl['e_l']}*.\nMarked as GAP.")
                if short_st == "PENDING" and lvl.get("e_s") and ltp < lvl["e_s"]:
                    logger.warning(
                        f"{inst}: POST-STARTUP GAP detected. LTP={ltp} already BELOW E_S={lvl['e_s']}. "
                        "Marking as GAP to prevent wrong entry after restart."
                    )
                    _set_state(inst, "short_state", "GAP")
                    short_st = "GAP"
                    tg.send_msg(f"⚡ *{inst} Startup GAP Detected*\nLTP *{ltp}* already below E_S *{lvl['e_s']}*.\nMarked as GAP.")

        # ── 1. 09:15 - 09:30: Track 15-min Range for Recovery ────────
        if "09:15:00" <= ts < "09:30:00":
            with _lock:
                _gap_window[inst]["high"] = max(_gap_window[inst]["high"], ltp)
                _gap_window[inst]["low"]  = min(_gap_window[inst]["low"], ltp)

        # ── 2. 09:15 - 09:16: One-Minute Gap Detection Window ───────
        if "09:15:00" <= ts < "09:16:00":
            if long_st == "PENDING" and lvl.get("e_l") and ltp >= lvl["e_l"]:
                _set_state(inst, "long_state", "GAP")
                logger.warning(f"{inst}: 1-MINUTE GAP UP detected at {ltp} (E_L={lvl['e_l']})")
                tg.send_gap(inst, "LONG", ltp, lvl["e_l"])
                long_st = "GAP" # Local update for this tick
            if short_st == "PENDING" and lvl.get("e_s") and ltp <= lvl["e_s"]:
                _set_state(inst, "short_state", "GAP")
                logger.warning(f"{inst}: 1-MINUTE GAP DOWN at {ltp} (E_S={lvl['e_s']})")
                tg.send_gap(inst, "SHORT", ltp, lvl["e_s"])
                short_st = "GAP" # Local update for this tick
            continue # No entries or SL checks in gap window

        # ── 3. 09:30: Gap Recovery & SL Reset ────────────────────────
        if ts == "09:30:05" or ("09:30:05" <= ts <= "09:30:15"):
            _handle_gap_recovery_930(inst, state, ltp)
            _handle_930_sl_reset(inst, state, ltp)

        # ── 4. Normal Monitoring (Only if NOT in GAP state) ──────────
        # Time lock: No entries or SL checks before 09:16 AM
        if ts < "09:16:00":
            continue

        # If in GAP state, we wait until 09:30 (handled by _handle_gap_recovery_930)
        if long_st == "GAP" or short_st == "GAP":
            continue

        # Long Entry — use gap entry level if present
        if long_st == "PENDING" and eff_e_l and ltp >= eff_e_l:
            _trigger_long(inst, ltp, lvl, state)

        # Short Entry — use gap entry level if present
        if short_st == "PENDING" and eff_e_s and ltp <= eff_e_s:
            _trigger_short(inst, ltp, lvl, state)
        
        # Long Monitoring
        if long_st == "ACTIVE_P1":
            sl = lvl.get("sl1_long", {}).get("sl")
            target = lvl.get("t_l")
            if sl and ltp <= sl: _close_long(inst, ltp, "SL_HIT", sl)
            elif target and ltp >= target: _hit_target_long(inst, ltp, target, lvl)
        elif long_st == "ACTIVE_P2":
            sl = lvl.get("sl2_long", {}).get("sl")
            if sl and ltp <= sl: _close_long(inst, ltp, "SL2_HIT", sl)

        # Short Monitoring
        if short_st == "ACTIVE_P1":
            sl = lvl.get("sl1_short", {}).get("sl")
            target = lvl.get("t_s")
            if sl and ltp >= sl: _close_short(inst, ltp, "SL_HIT", sl)
            elif target and ltp <= target: _hit_target_short(inst, ltp, target, lvl)
        elif short_st == "ACTIVE_P2":
            sl = lvl.get("sl2_short", {}).get("sl")
            if sl and ltp >= sl: _close_short(inst, ltp, "SL2_HIT", sl)

def _trigger_long(inst, ltp, lvl, state):
    # Use gap entry level if available, else fresh e_l
    entry_price = lvl.get("gap_e_l") or lvl.get("e_l")
    if not entry_price:
        return
    _set_state(inst, "long_state", "ACTIVE_P1")
    _set_state(inst, "long_entry_price", entry_price)
    entry_dt = _now_ist().isoformat()
    _set_state(inst, "long_entry_date", entry_dt)
    _recalculate_active_levels(_live[inst])
    live_lvl = _live[inst]["levels"]
    t_l = live_lvl["t_l"]
    sl1l = live_lvl["sl1_long"]["sl"]
    upsert_state(inst, {"levels_json": json.dumps(live_lvl)})
    logger.info(f"{inst}: LONG ENTRY triggered at level {entry_price} (ltp={ltp}) | Target: {t_l}")
    tg.send_entry_triggered(inst, "LONG", entry_price, t_l, sl1l, state["trading_symbol"])
    _send_to_main_app(inst, "BUY", entry_price, sl1l, [t_l], state, state.get("auto_trade", False))

def _trigger_short(inst, ltp, lvl, state):
    # Use gap entry level if available, else fresh e_s
    entry_price = lvl.get("gap_e_s") or lvl.get("e_s")
    if not entry_price:
        return
    _set_state(inst, "short_state", "ACTIVE_P1")
    _set_state(inst, "short_entry_price", entry_price)
    entry_dt = _now_ist().isoformat()
    _set_state(inst, "short_entry_date", entry_dt)
    _recalculate_active_levels(_live[inst])
    live_lvl = _live[inst]["levels"]
    t_s = live_lvl["t_s"]
    sl1s = live_lvl["sl1_short"]["sl"]
    upsert_state(inst, {"levels_json": json.dumps(live_lvl)})
    logger.info(f"{inst}: SHORT ENTRY triggered at level {entry_price} (ltp={ltp}) | Target: {t_s}")
    tg.send_entry_triggered(inst, "SHORT", entry_price, t_s, sl1s, state["trading_symbol"])
    _send_to_main_app(inst, "SELL", entry_price, sl1s, [t_s], state, state.get("auto_trade", False))

def _hit_target_long(inst, ltp, target, lvl):
    _set_state(inst, "long_lot1_closed", True)
    _set_state(inst, "long_state", "ACTIVE_P2")
    pnl = round((target - (_live[inst].get("long_entry_price") or target)) * 1 * MULTIPLIERS.get(inst, 25), 2)
    _set_state(inst, "long_pnl", pnl)
    tg.send_lot1_target_hit(inst, "LONG", target, pnl, lvl["sl2_long"]["sl"])
    from core.pnl_logger import log_closed_trade
    log_closed_trade(
        instrument=inst, trading_symbol=_live[inst].get("trading_symbol", inst),
        direction="LONG", entry_price=(_live[inst].get("long_entry_price") or target),
        exit_price=target, entry_date=_live[inst].get("long_entry_date", ""),
        exit_reason="PARTIAL_TARGET", lots=1, lot_size=MULTIPLIERS.get(inst, 25), strategy=STRATEGY_NAME
    )

def _hit_target_short(inst, ltp, target, lvl):
    _set_state(inst, "short_lot1_closed", True)
    _set_state(inst, "short_state", "ACTIVE_P2")
    pnl = round(((_live[inst].get("short_entry_price") or target) - target) * 1 * MULTIPLIERS.get(inst, 25), 2)
    _set_state(inst, "short_pnl", pnl)
    tg.send_lot1_target_hit(inst, "SHORT", target, pnl, lvl["sl2_short"]["sl"])
    from core.pnl_logger import log_closed_trade
    log_closed_trade(
        instrument=inst, trading_symbol=_live[inst].get("trading_symbol", inst),
        direction="SHORT", entry_price=(_live[inst].get("short_entry_price") or target),
        exit_price=target, entry_date=_live[inst].get("short_entry_date", ""),
        exit_reason="PARTIAL_TARGET", lots=1, lot_size=MULTIPLIERS.get(inst, 25), strategy=STRATEGY_NAME
    )

def _close_long(inst, ltp, reason, sl):
    entry = _live[inst].get("long_entry_price") or sl
    lot1done = _live[inst].get("long_lot1_closed")
    lots = 1 if lot1done else LOTS
    realized_lot1 = _live[inst].get("long_pnl", 0) if lot1done else 0
    pnl = round((ltp - entry) * lots * MULTIPLIERS.get(inst, 25), 2)
    if lot1done: pnl = round(realized_lot1 + pnl, 2)
    _set_state(inst, "long_state", "CLOSED"); _set_state(inst, "long_exit_price", ltp)
    _set_state(inst, "long_exit_reason", reason); _set_state(inst, "long_pnl", pnl)
    tg.send_sl_hit(inst, "LONG", sl, entry, pnl, "Exit")
    # Log to P&L Report
    log_closed_trade(
        instrument=inst,
        trading_symbol=_live[inst].get("trading_symbol", inst),
        direction="LONG",
        entry_price=entry,
        exit_price=ltp,
        entry_date=_live[inst].get("long_entry_date", ""),
        exit_reason=reason,
        lots=lots,
        lot_size=MULTIPLIERS.get(inst, 25),
        strategy=STRATEGY_NAME,
        realized_lot1_pnl=0, # Already logged separately
    )

def _close_short(inst, ltp, reason, sl):
    entry = _live[inst].get("short_entry_price") or sl
    lot1done = _live[inst].get("short_lot1_closed")
    lots = 1 if lot1done else LOTS
    realized_lot1 = _live[inst].get("short_pnl", 0) if lot1done else 0
    pnl = round((entry - ltp) * lots * MULTIPLIERS.get(inst, 25), 2)
    if lot1done: pnl = round(realized_lot1 + pnl, 2)
    _set_state(inst, "short_state", "CLOSED"); _set_state(inst, "short_exit_price", ltp)
    _set_state(inst, "short_exit_reason", reason); _set_state(inst, "short_pnl", pnl)
    tg.send_sl_hit(inst, "SHORT", sl, entry, pnl, "Exit")
    # Log to P&L Report
    log_closed_trade(
        instrument=inst,
        trading_symbol=_live[inst].get("trading_symbol", inst),
        direction="SHORT",
        entry_price=entry,
        exit_price=ltp,
        entry_date=_live[inst].get("short_entry_date", ""),
        exit_reason=reason,
        lots=lots,
        lot_size=MULTIPLIERS.get(inst, 25),
        strategy=STRATEGY_NAME,
        realized_lot1_pnl=0, # Already logged separately
    )

def _set_state(inst, key, val):
    with _lock: _live[inst][key] = val
    upsert_state(inst, {key: val})

def _handle_gap_recovery_930(inst: str, state: dict, ltp: float):
    """
    Triggered at 09:30 AM to handle Nifty Gap Recovery.
    Uses 15-min range (09:15-09:30) and 0.125% buffer.
    """
    today = _now_ist().date().isoformat()
    if state.get("last_gap_recovery_date") == today: return
    
    long_st = state.get("long_state")
    short_st = state.get("short_state")
    if long_st != "GAP" and short_st != "GAP": return
    
    with _lock:
        g_h = _gap_window[inst]["high"]
        g_l = _gap_window[inst]["low"]
    
    if g_h == 0 or g_l == 999999.0: return
    
    from .calculator import NiftyLevels
    raw_days = state["levels"].get("raw_days", [])
    nl = NiftyLevels(inst, state["trading_symbol"], state["token"], raw_days)
    
    if long_st == "GAP":
        new_e = rt(g_h * 1.00125)
        nl.update_from_actual_entry(new_e, "long")
        logger.info(f"{inst}: NIFTY LONG Gap Recovery at 9:30. Gap E_L: {new_e} | Dashboard E_L unchanged")
        tg.send_msg(f"✅ *NIFTY LONG GAP RECOVERY (9:30 AM)*\nGap Long Entry: *{new_e}*\nTarget: *{nl.t_l}*\nSL1: *{nl.sl1_long['sl']}*")
        _set_state(inst, "long_state", "PENDING")
        _set_state(inst, "long_gap_recovered", True)
        # Store gap entry in gap_e_l — do NOT overwrite e_l (dashboard stays clean)
        state["levels"]["gap_e_l"] = new_e
        state["levels"]["gap_t_l"] = nl.t_l

    if short_st == "GAP":
        new_e = rt(g_l * 0.99875)
        nl.update_from_actual_entry(new_e, "short")
        logger.info(f"{inst}: NIFTY SHORT Gap Recovery at 9:30. Gap E_S: {new_e} | Dashboard E_S unchanged")
        tg.send_msg(f"✅ *NIFTY SHORT GAP RECOVERY (9:30 AM)*\nGap Short Entry: *{new_e}*\nTarget: *{nl.t_s}*\nSL1: *{nl.sl1_short['sl']}*")
        _set_state(inst, "short_state", "PENDING")
        _set_state(inst, "short_gap_recovered", True)
        # Store gap entry in gap_e_s — do NOT overwrite e_s (dashboard stays clean)
        state["levels"]["gap_e_s"] = new_e
        state["levels"]["gap_t_s"] = nl.t_s

    # Build new levels preserving fresh e_l/e_s, only update SL/target from recovery
    new_lvl = nl.to_dict()
    # Restore original fresh e_l/e_s so dashboard doesn't change
    orig_lvl = state.get("levels", {})
    if orig_lvl.get("e_l"): new_lvl["e_l"] = orig_lvl["e_l"]
    if orig_lvl.get("e_s"): new_lvl["e_s"] = orig_lvl["e_s"]
    if orig_lvl.get("gap_e_l"): new_lvl["gap_e_l"] = orig_lvl["gap_e_l"]
    if orig_lvl.get("gap_e_s"): new_lvl["gap_e_s"] = orig_lvl["gap_e_s"]
    if orig_lvl.get("gap_t_l"): new_lvl["gap_t_l"] = orig_lvl["gap_t_l"]
    if orig_lvl.get("gap_t_s"): new_lvl["gap_t_s"] = orig_lvl["gap_t_s"]
    _set_state(inst, "levels", new_lvl)
    _set_state(inst, "last_gap_recovery_date", today)
    upsert_state(inst, {"levels_json": json.dumps(new_lvl)})

def _handle_930_sl_reset(inst: str, state: dict, ltp: float):
    """
    Rule: At 09:30 AM, if an active trade's SL was breached by the opening 15-min range, reset SL.
    Buffer: 0.125% for Nifty.
    """
    today = _now_ist().date().isoformat()
    if state.get("last_sl_reset_date") == today: return

    with _lock:
        g_h = _gap_window[inst]["high"]
        g_l = _gap_window[inst]["low"]
    
    if g_h == 0 or g_l == 999999.0: return

    lvl = state.get("levels", {})
    changed = False
    
    # Check SHORT SL breach
    if state["short_state"] in ("ACTIVE_P1", "ACTIVE_P2"):
        sl_key = "sl1_short" if state["short_state"] == "ACTIVE_P1" else "sl2_short"
        curr_sl = lvl.get(sl_key, {}).get("sl", 0)
        if curr_sl and g_h >= curr_sl:
            new_sl = rt(g_h * 1.00125)
            lvl[sl_key]["sl"] = new_sl
            lvl[sl_key]["b"] = new_sl
            changed = True
            logger.info(f"{inst}: NIFTY SHORT SL Gap Reset (9:30). New SL: {new_sl}")
            tg.send_msg(f"🔔 *NIFTY SL RESET (9:30 AM)*\nShort SL was breached by opening range. New SL set at *{new_sl}* (High + 0.125%)")

    # Check LONG SL breach
    if state["long_state"] in ("ACTIVE_P1", "ACTIVE_P2"):
        sl_key = "sl1_long" if state["long_state"] == "ACTIVE_P1" else "sl2_long"
        curr_sl = lvl.get(sl_key, {}).get("sl", 0)
        if curr_sl and g_l <= curr_sl:
            new_sl = rt(g_l * 0.99875)
            lvl[sl_key]["sl"] = new_sl
            lvl[sl_key]["b"] = new_sl
            changed = True
            logger.info(f"{inst}: NIFTY LONG SL Gap Reset (9:30). New SL: {new_sl}")
            tg.send_msg(f"🔔 *NIFTY SL RESET (9:30 AM)*\nLong SL was breached by opening range. New SL set at *{new_sl}* (Low - 0.125%)")

    if changed:
        _set_state(inst, "levels", lvl)
        _set_state(inst, "last_sl_reset_date", today)
        upsert_state(inst, {"levels_json": json.dumps(lvl)})

def _send_to_main_app(inst, action, price, sl, targets, state, auto):
    if not auto: return
    try:
        from core.engine import engine
        payload = {
            "symbol": state["trading_symbol"], "exchange": "NSE", "instrument_type": "FUTIDX",
            "action": action, "entry_price": price, "stop_loss": sl, "targets": targets,
            "quantity": LOTS * MULTIPLIERS.get(inst, 25), "lot_size": 1, "trade_type": "POSITIONAL", "strategy": STRATEGY_NAME,
        }
        engine.add_trade(payload, lot_size=1, strategy=STRATEGY_NAME)
    except Exception as e: logger.error(f"Trade error: {e}")

def get_all_live():
    with _lock:
        return dict(_live)

_thread = None
def start_monitor():
    global _thread
    if _thread and _thread.is_alive(): return
    def _loop():
        logger.info(f"{STRATEGY_NAME} monitor running")
        while True:
            try:
                ts = _time_str()
                _monitor_tick()
            except Exception as e: logger.error(f"{STRATEGY_NAME} monitor error: {e}")
            time.sleep(10)
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
