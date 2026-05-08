"""
Silver Strategy Scheduler
=========================
• Morning Session: 7:50 AM (Briefing), 9:00 AM (Target), 9:10 AM (Entry/SL), 9:15 AM (Gap Recovery)
• Evening Session: 17:00 PM (Target), 17:10 PM (Entry/SL), 17:15 PM (Gap Recovery)
  Note: Evening session only acts if morning session was closed (Holiday).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from loguru import logger

from .data_fetcher  import fetch_instrument_data, INSTRUMENTS
from .calculator    import SilverLevels, rt
from .monitor       import set_levels_from_silver_levels, get_live_state, _set_state
from .database      import get_setting, set_setting
from . import telegram as tg

IST = pytz.timezone("Asia/Kolkata")

# ─── 7:50 AM — Fetch data + Telegram morning briefing ────────────────────────

def _fetch_and_broadcast(broadcast=None):
    """Fetch data, calculate levels for all instruments, send Telegram."""
    logger.info(f"Silver Strategy: Data fetch starting (requested_broadcast={broadcast})...")
    levels_map = {}
    live_states = {inst: get_live_state(inst) for inst in INSTRUMENTS}

    for inst in INSTRUMENTS:
        try:
            dual_data = fetch_instrument_data(inst)
            if not dual_data or not dual_data.get("current"):
                logger.warning(f"No data for {inst}")
                levels_map[inst] = None
                continue
                
            data = dual_data["current"]
            # TODO: Handle dual_data["next"] for rollover window
            
            gl = SilverLevels(
                instrument=inst,
                trading_symbol=data["trading_symbol"],
                token=data["token"],
                raw_days=data["candles"],
            )
            set_levels_from_silver_levels(inst, gl)

            # For Telegram: use live state's levels (has correct trailing SL2)
            # instead of fresh gl.to_dict() which doesn't have ratchet applied.
            state = live_states.get(inst, {})
            l_st = state.get("long_state", "PENDING")
            s_st = state.get("short_state", "PENDING")
            if l_st in ("ACTIVE_P1", "ACTIVE_P2") or s_st in ("ACTIVE_P1", "ACTIVE_P2"):
                levels_map[inst] = state.get("levels") or gl.to_dict()
            else:
                levels_map[inst] = gl.to_dict()

            logger.info(f"{inst}: E_L={gl.e_l}  E_S={gl.e_s}  SL1_L={gl.sl1_long}  SL1_S={gl.sl1_short}")
        except Exception as e:
            logger.error(f"Fetch failed for {inst}: {e}")
            levels_map[inst] = None

    # Smart broadcast logic
    should_broadcast = False
    if broadcast is True:
        should_broadcast = True
    elif broadcast is False:
        should_broadcast = False
    else:
        # Auto-detect mode (for startup/restarts)
        now = datetime.now(IST)
        today = now.date().isoformat()
        
        # Morning: 08:30 AM
        if now.hour == 8 and now.minute >= 25:
             should_broadcast = True
             logger.info(f"Silver Strategy: Broadcasting morning briefing (Time: {now.strftime('%H:%M')})")

    if should_broadcast:
        tg.send_morning_alert(levels_map, live_states=live_states)
        # Ensure we record that it was sent today
        now = datetime.now(IST)
        set_setting("last_morning_briefing_date", now.date().isoformat())


# ─── 8:50 AM — Re-verify data ────────────────────────────────────────────────

def _reverify():
    """8:50 AM — re-fetch data silently (no Telegram resend unless changed)."""
    logger.info("Silver Strategy: 8:50 AM re-verification...")
    for inst in INSTRUMENTS:
        try:
            data = fetch_instrument_data(inst)
            if not data:
                continue
            gl = SilverLevels(
                instrument=inst,
                trading_symbol=data["trading_symbol"],
                token=data["token"],
                raw_days=data["candles"],
            )
            set_levels_from_silver_levels(inst, gl)
        except Exception as e:
            logger.error(f"Re-verify failed for {inst}: {e}")


# ─── 9:00 AM / 5:00 PM — Place TARGET orders ─────────────────────────────────

def _place_target_orders():
    """
    Place Lot-1 TARGET limit order for instruments in PENDING state.
    Checks for market open before queuing.
    """
    logger.info("Silver Strategy: Session open — checking target orders...")
    for inst in INSTRUMENTS:
        state = get_live_state(inst)
        if not state or not state.get("token"): continue

        # Verify if market is actually open (has 1-min data for today)
        from .data_fetcher import angel_api
        now_ist = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")
        raw = angel_api.get_candle_data(token=state["token"], exchange="MCX", interval="ONE_MINUTE",
                                       from_date=f"{today_str} 09:00", to_date=now_ist.strftime("%Y-%m-%d %H:%M"))
        if not raw:
            logger.info(f"{inst}: No data yet today. Skipping this target order window.")
            continue

        lvl = state.get("levels", {})
        auto = state.get("auto_trade", False)

        # Long: PENDING → pre-place target sell order
        if state.get("long_state") == "PENDING":
            t_l = lvl.get("t_l", 0)
            if t_l:
                logger.info(f"{inst}: Pre-placing LONG Lot-1 target @ {t_l}")
                # tg.send(f"⏰ <b>{inst}</b> Target order queued @ <b>{t_l:,.2f}</b>")

        # Short: PENDING → pre-place target buy order
        if state.get("short_state") == "PENDING":
            t_s = lvl.get("t_s", 0)
            if t_s:
                logger.info(f"{inst}: Pre-placing SHORT Lot-1 target @ {t_s}")
                # tg.send(f"⏰ <b>{inst}</b> Target order queued @ <b>{t_s:,.2f}</b>")


# ─── 9:10 AM / 5:10 PM — Place ENTRY + SL orders ─────────────────────────────

def _place_entry_and_sl_orders():
    """
    Place entry/SL orders or update trailing SL2.
    Checks for market open before proceeding.
    """
    logger.info("Silver Strategy: placing entry/SL orders...")

    for inst in INSTRUMENTS:
        state = get_live_state(inst)
        if not state or not state.get("token"): continue

        # Verify if market is open before placing entry
        from .data_fetcher import angel_api
        now_ist = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")
        raw = angel_api.get_candle_data(token=state["token"], exchange="MCX", interval="ONE_MINUTE",
                                       from_date=f"{today_str} 09:00", to_date=now_ist.strftime("%Y-%m-%d %H:%M"))
        if not raw:
            logger.info(f"{inst}: No market data. Entry order aborted for this window.")
            continue

        lvl    = state.get("levels", {})
        auto   = state.get("auto_trade", False)
        l_st   = state.get("long_state")
        s_st   = state.get("short_state")

        # ── Case 1: Fresh entry + SL ─────────────────────────────────
        if l_st == "PENDING":
            e_l  = lvl.get("e_l", 0)
            sl1l = lvl.get("sl1_long",  {}).get("sl", 0)
            if e_l and sl1l:
                logger.info(f"{inst}: LONG entry order @ {e_l} | SL @ {sl1l}")
                # tg.send(f"🕙 <b>{inst}</b>\n⬆️ Long Entry order @ <b>{e_l:,.2f}</b>\n🛡 SL1 order @ <b>{sl1l:,.2f}</b>")

        if s_st == "PENDING":
            e_s  = lvl.get("e_s", 0)
            sl1s = lvl.get("sl1_short", {}).get("sl", 0)
            if e_s and sl1s:
                logger.info(f"{inst}: SHORT entry order @ {e_s} | SL @ {sl1s}")
                # tg.send(f"🕙 <b>{inst}</b>\n⬇️ Short Entry order @ <b>{e_s:,.2f}</b>\n🛡 SL1 order @ <b>{sl1s:,.2f}</b>")

        # ── Case 2: Overnight Lot-2 — update trailing SL2 ────────────
        if l_st == "ACTIVE_P2":
            _update_trailing_sl2(inst, state, "long", auto)
        if s_st == "ACTIVE_P2":
            _update_trailing_sl2(inst, state, "short", auto)

        # ── Case 3: Phase 1 (Before Target) — update trailing SL1 ──
        if l_st == "ACTIVE_P1":
            _update_trailing_sl1(inst, state, "long", auto)
        if s_st == "ACTIVE_P1":
            _update_trailing_sl1(inst, state, "short", auto)

def _handle_gap_recovery():
    """
    If an instrument is in GAP state, fetch the 15-min candle,
    recalculate levels for the GAPPED side, and place new orders.
    """
    logger.info("Silver Strategy: checking for gap recovery...")
    from .data_fetcher import angel_api
    
    for inst in INSTRUMENTS:
        state = get_live_state(inst)
        if not state: continue
        
        ls = state.get("long_state")
        ss = state.get("short_state")
        
        if ls != "GAP" and ss != "GAP":
            continue
            
        logger.info(f"{inst}: Gap recovery triggered! Fetching 15-min candle...")
        
        # Fetch 15-min candle for today (either 09:00 or 17:00)
        now_ist = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")
        curr_hour = now_ist.hour
        open_time = "17:00" if curr_hour >= 17 else "09:00"
        open_dt = f"{today_str} {open_time}"
        
        raw = angel_api.get_candle_data(
            token=state["token"], exchange="MCX", interval="FIFTEEN_MINUTE",
            from_date=open_dt, to_date=now_ist.strftime("%Y-%m-%d %H:%M")
        )
        
        if not raw or len(raw) == 0:
            logger.error(f"{inst}: Could not fetch 15-min candle for gap recovery")
            continue
            
        c15 = raw[0]
        h15 = float(c15[2])
        l15 = float(c15[3])
        logger.info(f"{inst}: 15-min candle High={h15} Low={l15}")
        
        # 1. GAP RECOVERY (Side-Specific)
        if ls == "GAP" or ss == "GAP":
            lvl = state["levels"]
            
            if ls == "GAP":
                new_e_l = rt(h15 * 1.0012)
                new_t_l = rt(new_e_l * 1.02)
                pt_a_l1 = rt(new_e_l * 0.98); pt_b_l1 = lvl["sl1_long"]["b"]; sl1_l = max(pt_a_l1, pt_b_l1)
                pt_a_l2 = rt(new_e_l * 0.98); pt_b_l2 = lvl["sl2_long"]["b"]; sl2_l = max(pt_a_l2, pt_b_l2)
                
                lvl["e_l"] = new_e_l; lvl["t_l"] = new_t_l
                lvl["sl1_long"] = {"sl": sl1_l, "a": pt_a_l1, "b": pt_b_l1}
                lvl["sl2_long"] = {"sl": sl2_l, "a": pt_a_l2, "b": pt_b_l2}
                _set_state(inst, "long_state", "PENDING")
                logger.info(f"{inst}: LONG Gap Recovery complete. New E_L={new_e_l}")

            if ss == "GAP":
                new_e_s = rt(l15 * 0.9988)
                new_t_s = rt(new_e_s * 0.98)
                pt_a_s1 = rt(new_e_s * 1.02); pt_b_s1 = lvl["sl1_short"]["b"]; sl1_s = min(pt_a_s1, pt_b_s1)
                pt_a_s2 = rt(new_e_s * 1.02); pt_b_s2 = lvl["sl2_short"]["b"]; sl2_s = min(pt_a_s2, pt_b_s2)
                
                lvl["e_s"] = new_e_s; lvl["t_s"] = new_t_s
                lvl["sl1_short"] = {"sl": sl1_s, "a": pt_a_s1, "b": pt_b_s1}
                lvl["sl2_short"] = {"sl": sl2_s, "a": pt_a_s2, "b": pt_b_s2}
                _set_state(inst, "short_state", "PENDING")
                logger.info(f"{inst}: SHORT Gap Recovery complete. New E_S={new_e_s}")

            _set_state(inst, "levels", lvl)
            tg.send(f"🔄 <b>{inst} Gap Recovery</b>\nNew levels calculated from 15-min open candle.")

        # 2. SL PROTECTION (for Active Trades)
        for side in ["long", "short"]:
            cur_st = state[f"{side}_state"]
            if cur_st in ["ACTIVE_P1", "ACTIVE_P2"]:
                phase = "sl1" if cur_st == "ACTIVE_P1" else "sl2"
                old_sl = state["levels"][f"{phase}_{side}"]["sl"]
                is_hit = (side == "long" and l15 < old_sl) or (side == "short" and h15 > old_sl)
                if is_hit:
                    logger.info(f"{inst}: {side} SL {old_sl} breached at open. Applying Gap Protection...")
                    new_sl = rt(l15 * 0.9988) if side == "long" else rt(h15 * 1.0012)
                    lvl = state["levels"]
                    lvl[f"{phase}_{side}"]["sl"] = new_sl
                    lvl[f"{phase}_{side}"]["b"] = new_sl
                    _set_state(inst, "levels", lvl)
                    tg.send(f"🛡️ <b>{inst} SL Protection</b>\nPrice opened beyond SL. New Emergency SL set.")

    _place_entry_and_sl_orders()


def _update_trailing_sl2(inst: str, state: dict, direction: str, auto: bool):
    """Recompute SL2 using rolling 4-day window (D-1 to D-4)."""
    try:
        from .data_fetcher import fetch_instrument_data
        data = fetch_instrument_data(inst)
        if not data or len(data["candles"]) < 4: return

        candles = data["candles"][:4]
        lvl = state.get("levels", {})
        
        current_sl2 = lvl.get("sl2_long" if direction == "long" else "sl2_short", {}).get("sl", 0)

        if direction == "long":
            e_l = state.get("long_entry_price") or lvl.get("e_l", 0)
            new_l4 = min(d["low"] for d in candles)
            pt_a = rt(e_l * 0.98); pt_b = rt(new_l4 * 0.9988); new_sl = max(pt_a, pt_b)
            if new_sl > current_sl2:
                tg.send_sl2_updated(inst, "LONG", current_sl2, new_sl, f"L4={new_l4}")
                lvl["sl2_long"]["sl"] = new_sl
                lvl["sl2_long"]["a"] = pt_a
                lvl["sl2_long"]["b"] = pt_b
                _set_state(inst, "levels", lvl)
        else:
            e_s = state.get("short_entry_price") or lvl.get("e_s", 0)
            new_h4 = max(d["high"] for d in candles)
            # Use stored 'a' (theoretical e_s*1.02) — do NOT override with actual entry
            stored_a = lvl.get("sl2_short", {}).get("a") or rt(e_s * 1.02)
            pt_b = rt(new_h4 * 1.0012)
            new_sl = min(stored_a, pt_b)
            if new_sl < current_sl2:
                tg.send_sl2_updated(inst, "SHORT", current_sl2, new_sl, f"H4={new_h4}")
                lvl["sl2_short"]["sl"] = new_sl
                lvl["sl2_short"]["b"] = pt_b
                # Leave 'a' unchanged
                _set_state(inst, "levels", lvl)
    except Exception as e:
        logger.error(f"SL2 update failed: {e}")


def _update_trailing_sl1(inst: str, state: dict, direction: str, auto: bool):
    """Recompute SL1 using rolling 2-day window (D-1, D-2)."""
    try:
        from .data_fetcher import fetch_instrument_data
        data = fetch_instrument_data(inst)
        if not data or len(data["candles"]) < 2: return
        two = data["candles"][:2]
        h2 = max(d["high"] for d in two); l2 = min(d["low"]  for d in two)
        lvl = state.get("levels", {})
        current_sl1 = lvl.get("sl1_long" if direction == "long" else "sl1_short", {}).get("sl", 0)

        if direction == "long":
            e_l = state.get("long_entry_price") or lvl.get("e_l", 0)
            pt_a = rt(e_l * 0.98); pt_b = rt(l2 * 0.9988); new_sl = max(pt_a, pt_b)
            if new_sl > current_sl1:
                lvl["sl1_long"]["sl"] = new_sl
                lvl["sl1_long"]["a"] = pt_a
                lvl["sl1_long"]["b"] = pt_b
                _set_state(inst, "levels", lvl)
        else:
            e_s = state.get("short_entry_price") or lvl.get("e_s", 0)
            pt_a = rt(e_s * 1.02); pt_b = rt(h2 * 1.0012); new_sl = min(pt_a, pt_b)
            if new_sl < current_sl1:
                lvl["sl1_short"]["sl"] = new_sl
                lvl["sl1_short"]["a"] = pt_a
                lvl["sl1_short"]["b"] = pt_b
                _set_state(inst, "levels", lvl)
    except Exception as e:
        logger.error(f"SL1 update failed: {e}")


# ─── Scheduler setup ─────────────────────────────────────────────────────────

def start_scheduler():
    sched = BackgroundScheduler(timezone=IST)
    
    # 1. Fetch Data - 08:05 AM (Mon-Fri)
    sched.add_job(_fetch_and_broadcast, "cron", day_of_week="mon-fri", hour=8, minute=5, args=[False], id="morning_fetch")
    
    # 2. Morning Briefing - 08:30 AM (Mon-Fri)
    sched.add_job(_fetch_and_broadcast, "cron", day_of_week="mon-fri", hour=8, minute=30, args=[True], id="morning_broadcast")
    
    sched.add_job(_reverify, "cron", day_of_week="mon-fri", hour=8, minute=50, id="reverify")

    # Morning Session
    sched.add_job(_place_target_orders, "cron", day_of_week="mon-fri", hour=9, minute=0, id="morning_target")
    sched.add_job(_place_entry_and_sl_orders, "cron", day_of_week="mon-fri", hour=9, minute=10, id="morning_entry")
    sched.add_job(_handle_gap_recovery, "cron", day_of_week="mon-fri", hour=9, minute=15, id="morning_gap")

    # Evening Session
    sched.add_job(_place_target_orders, "cron", day_of_week="mon-fri", hour=17, minute=1, id="evening_target")
    sched.add_job(_place_entry_and_sl_orders, "cron", day_of_week="mon-fri", hour=17, minute=10, id="evening_entry")
    sched.add_job(_handle_gap_recovery, "cron", day_of_week="mon-fri", hour=17, minute=15, id="evening_gap")

    sched.start()
    logger.info("Silver Strategy scheduler started (Morning 9:00 | Evening 17:01 support)")
    return sched

def fetch_now(broadcast=None):
    _fetch_and_broadcast(broadcast=broadcast)
