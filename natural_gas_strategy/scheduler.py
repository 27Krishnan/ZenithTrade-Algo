"""
Natural Gas Strategy Scheduler
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
from .calculator    import NaturalGasLevels, rt
from .monitor       import set_levels_from_natural_gas_levels, get_live_state, _set_state
from .database      import get_setting, set_setting
from . import telegram as tg

IST = pytz.timezone("Asia/Kolkata")

# ─── 7:50 AM — Fetch data + Telegram morning briefing ────────────────────────

def _fetch_and_broadcast(broadcast=None):
    """Fetch data, calculate levels for all instruments, send Telegram."""
    logger.info(f"Natural Gas Strategy: Data fetch starting (requested_broadcast={broadcast})...")
    levels_map = {}
    for inst in INSTRUMENTS:
        try:
            data = fetch_instrument_data(inst)
            if not data:
                logger.warning(f"No data for {inst}")
                levels_map[inst] = None
                continue
            # fetch_instrument_data returns {"current": {...}, "next": {...}}
            curr = data.get("current") or data
            gl = NaturalGasLevels(
                instrument=inst,
                trading_symbol=curr["trading_symbol"],
                token=curr["token"],
                raw_days=curr["candles"],
            )
            set_levels_from_natural_gas_levels(inst, gl)
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
             logger.info(f"Natural Gas Strategy: Broadcasting morning briefing (Time: {now.strftime('%H:%M')})")

    if should_broadcast:
        tg.send_morning_alert(levels_map, live_states={
            inst: get_live_state(inst) for inst in INSTRUMENTS
        })
        # Ensure we record that it was sent today
        now = datetime.now(IST)
        set_setting("last_morning_briefing_date", now.date().isoformat())


# ─── 8:50 AM — Re-verify data ────────────────────────────────────────────────

def _reverify():
    """8:50 AM — re-fetch data silently (no Telegram resend unless changed)."""
    logger.info("Natural Gas Strategy: 8:50 AM re-verification...")
    for inst in INSTRUMENTS:
        try:
            data = fetch_instrument_data(inst)
            if not data:
                continue
            gl = NaturalGasLevels(
                instrument=inst,
                trading_symbol=data["trading_symbol"],
                token=data["token"],
                raw_days=data["candles"],
            )
            set_levels_from_natural_gas_levels(inst, gl)
        except Exception as e:
            logger.error(f"Re-verify failed for {inst}: {e}")


# ─── 9:00 AM — Place TARGET orders ───────────────────────────────────────────

def _place_target_orders():
    """
    9:00 AM — Place Lot-1 TARGET limit order for instruments in PENDING state.
    Only Lot-1 has a fixed target. Lot-2 exits on trailing SL2 only.
    This goes out BEFORE the gap window closes, so exchange holds it but
    our logic will cancel/ignore if a gap is detected at 9:10.
    """
    logger.info("Natural Gas Strategy: 9:00 AM — placing target orders...")
    for inst in INSTRUMENTS:
        # Check if market is actually open (has 1-min data for today)
        state = get_live_state(inst)
        if not state or not state.get("token"): continue
        
        # Verify if morning or evening open
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
                # tg.send(f"⏰ <b>{inst}</b> 9:00 AM\nLot-1 Long Target order queued @ <b>{t_l:,.2f}</b>\n(Entry + SL order → 9:10 AM)")
                # Auto-trade: would place limit sell order here when order system ready
                # _place_limit_order(inst, "SELL", t_l, qty=1, auto=auto)

        # Short: PENDING → pre-place target buy order
        if state.get("short_state") == "PENDING":
            t_s = lvl.get("t_s", 0)
            if t_s:
                logger.info(f"{inst}: Pre-placing SHORT Lot-1 target @ {t_s}")
                # tg.send(f"⏰ <b>{inst}</b> 9:00 AM\nLot-1 Short Target order queued @ <b>{t_s:,.2f}</b>\n(Entry + SL order → 9:10 AM)")


# ─── 9:10 AM — Place ENTRY + SL orders (and update trailing SL2 if overnight) ─

def _place_entry_and_sl_orders():
    """
    9:10 AM — Two cases:

    Case 1 — Fresh trade (state is PENDING after gap check):
        • Gap window just closed (9:00–9:10 already monitored by monitor.py).
        • If still PENDING (no gap detected) → place Entry order + SL1 order.

    Case 2 — Overnight Lot-2 (state is ACTIVE_P2 from previous day):
        • Recalculate rolling SL2 using latest 3-day window.
        • SL2 only moves in the favourable direction (trailing).
        • Cancel previous SL order and place new SL order at updated level.
        • Send Telegram alert with new SL value.
    """
    logger.info("Natural Gas Strategy: 9:10 AM — placing entry/SL orders (and updating trailing SL2)...")

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

        # ── Case 1: Fresh entry + SL at 9:10 ─────────────────────────
        if l_st == "PENDING":
            e_l  = lvl.get("e_l", 0)
            sl1l = lvl.get("sl1_long",  {}).get("sl", 0)
            if e_l and sl1l:
                logger.info(f"{inst}: 9:10 AM — LONG entry order @ {e_l} | SL @ {sl1l}")
                # tg.send(f"🕙 <b>{inst} 9:10 AM</b>\n"
                #         f"⬆️ Long Entry order placed @ <b>{e_l:,.2f}</b>\n"
                #         f"🛡 SL1 order placed @ <b>{sl1l:,.2f}</b>")

        if s_st == "PENDING":
            e_s  = lvl.get("e_s", 0)
            sl1s = lvl.get("sl1_short", {}).get("sl", 0)
            if e_s and sl1s:
                logger.info(f"{inst}: 9:10 AM - SHORT entry order @ {e_s} | SL @ {sl1s}")
                # tg.send(f"🕙 <b>{inst} 9:10 AM</b>\n"
                #         f"⬇️ Short Entry order placed @ <b>{e_s:,.2f}</b>\n"
                #         f"🛡 SL1 order placed @ <b>{sl1s:,.2f}</b>")

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
    9:15 AM — If an instrument is in GAP state, fetch the 9:00-9:15 candle,
    recalculate levels using its High/Low, and place new orders.
    """
    logger.info("Natural Gas Strategy: 9:15 AM — checking for gap recovery...")
    from .data_fetcher import angel_api
    
    for inst in INSTRUMENTS:
        state = get_live_state(inst)
        if not state: continue
        
        ls = state.get("long_state")
        ss = state.get("short_state")
        
        if ls != "GAP" and ss != "GAP":
            continue
            
        logger.info(f"{inst}: Gap recovery triggered! Fetching 15-min open candle...")

        # Fetch the first 15-min candle of the active session.
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
            
        # [timestamp, open, high, low, close, volume]
        c15 = raw[0]
        h15 = float(c15[2])
        l15 = float(c15[3])
        logger.info(f"{inst}: 15-min candle (9:00-9:15) High={h15} Low={l15}")
        
        # 1. GAP RECOVERY (for Pending Trades)
        if ls == "GAP" or ss == "GAP":
            logger.info(f"{inst}: Processing Gap Recovery...")
            new_e_l = rt(h15 * 1.004)
            new_e_s = rt(l15 * 0.996)
            new_t_l = rt(new_e_l * 1.04)
            new_t_s = rt(new_e_s * 0.96)
            
            # SL1/SL2 recalculated using new Entry
            pt_a_l1 = rt(new_e_l * 0.96); pt_b_l1 = state["levels"]["sl1_long"]["b"]; sl1_l = max(pt_a_l1, pt_b_l1)
            pt_a_s1 = rt(new_e_s * 1.04); pt_b_s1 = state["levels"]["sl1_short"]["b"]; sl1_s = min(pt_a_s1, pt_b_s1)
            pt_a_l2 = rt(new_e_l * 0.96); pt_b_l2 = state["levels"]["sl2_long"]["b"]; sl2_l = max(pt_a_l2, pt_b_l2)
            pt_a_s2 = rt(new_e_s * 1.04); pt_b_s2 = state["levels"]["sl2_short"]["b"]; sl2_s = min(pt_a_s2, pt_b_s2)

            lvl = state["levels"]
            lvl["e_l"] = new_e_l; lvl["e_s"] = new_e_s
            lvl["t_l"] = new_t_l; lvl["t_s"] = new_t_s
            lvl["sl1_long"] = {"sl": sl1_l, "a": pt_a_l1, "b": pt_b_l1}
            lvl["sl1_short"] = {"sl": sl1_s, "a": pt_a_s1, "b": pt_b_s1}
            lvl["sl2_long"] = {"sl": sl2_l, "a": pt_a_l2, "b": pt_b_l2}
            lvl["sl2_short"] = {"sl": sl2_s, "a": pt_a_s2, "b": pt_b_s2}
            
            if ls == "GAP": _set_state(inst, "long_state", "PENDING")
            if ss == "GAP": _set_state(inst, "short_state", "PENDING")
            _set_state(inst, "levels", lvl)
            
            tg.send(f"🔄 <b>{inst} Gap Recovery</b>\n"
                    f"New levels calculated from 9:15 AM candle.\n"
                    f"Entry: {new_e_l} / {new_e_s}")

        # 2. SL PROTECTION (for Active Trades)
        # If in a trade and price is beyond SL at 9:15 AM opening, reset SL using 9:15 candle
        for side in ["long", "short"]:
            cur_st = state[f"{side}_state"]
            if cur_st in ["ACTIVE_P1", "ACTIVE_P2"]:
                phase = "sl1" if cur_st == "ACTIVE_P1" else "sl2"
                old_sl = state["levels"][f"{phase}_{side}"]["sl"]
                
                is_hit = (side == "long" and l15 < old_sl) or (side == "short" and h15 > old_sl)
                if is_hit:
                    logger.info(f"{inst}: {side} SL {old_sl} breached at 9:15 open. Applying Gap Protection...")
                    new_sl = rt(l15 * 0.996) if side == "long" else rt(h15 * 1.004)
                    
                    lvl = state["levels"]
                    lvl[f"{phase}_{side}"]["sl"] = new_sl
                    lvl[f"{phase}_{side}"]["b"] = new_sl
                    _set_state(inst, "levels", lvl)
                    
                    logger.info(f"{inst}: {side} SL updated to {new_sl} due to gap protection.")
                    tg.send(f"🛡️ <b>{inst} SL Protection</b>\n"
                            f"Price opened beyond SL. New Emergency SL set from 9:15 AM candle.\n"
                            f"Old SL: {old_sl} → New SL: {new_sl}")

    # Now call the regular order placement/update logic
    _place_entry_and_sl_orders()


def _update_trailing_sl2(inst: str, state: dict, direction: str, auto: bool):
    """
    Recompute SL2 using the rolling 3-day window (fetched fresh this morning),
    trail it only in the favourable direction, and place updated SL order at 9:10 AM.

    Rolling window: today's 3 days = days [D-1, D-2, D-3]
    (The entry day and any subsequent days are now INCLUDED in the lookback.)
    """
    try:
        from .data_fetcher import fetch_instrument_data
        data = fetch_instrument_data(inst)
        if not data or len(data["candles"]) < 3:
            logger.warning(f"{inst}: Not enough candles for trailing SL2 update")
            return

        candles = data["candles"][:3]  # newest 3 completed days
        lvl = state.get("levels", {})
        e_l = state.get("long_entry_price") or lvl.get("e_l", 0)
        e_s = state.get("short_entry_price") or lvl.get("e_s", 0)

        # Current SL2 stored in DB
        current_sl2 = lvl.get(
            "sl2_long" if direction == "long" else "sl2_short", {}
        ).get("sl", 0)

        if direction == "long":
            new_l3 = min(d["low"] for d in candles)
            pt_a   = rt(e_l * 0.96)
            pt_b   = rt(new_l3 * 0.996)
            new_sl = max(pt_a, pt_b)
            # Trailing: only move UP for long
            if new_sl > current_sl2:
                logger.info(f"{inst}: LONG SL2 trailing UP {current_sl2:.2f} -> {new_sl:.2f}  (L3={new_l3})")
                window = f"L3={new_l3:,.2f}  [A={pt_a:,.2f}  B={pt_b:,.2f}]"
                tg.send_sl2_updated(inst, "LONG", current_sl2, new_sl, window)
                # Update in DB/memory
                lvl["sl2_long"]["sl"] = new_sl
                lvl["sl2_long"]["a"] = pt_a
                lvl["sl2_long"]["b"] = pt_b
                _set_state(inst, "levels", lvl)
            else:
                logger.info(f"{inst}: LONG SL2 unchanged (new={new_sl:.2f} ≤ current={current_sl2:.2f}) — locked in")
                tg.send_sl2_locked(inst, "LONG", current_sl2)

        else:  # short
            new_h3 = max(d["high"] for d in candles)
            pt_a   = rt(e_s * 1.04)
            pt_b   = rt(new_h3 * 1.004)
            new_sl = min(pt_a, pt_b)
            # Trailing: only move DOWN for short
            if new_sl < current_sl2:
                logger.info(f"{inst}: SHORT SL2 trailing DOWN {current_sl2:.2f} -> {new_sl:.2f}  (H3={new_h3})")
                window = f"H3={new_h3:,.2f}  [A={pt_a:,.2f}  B={pt_b:,.2f}]"
                tg.send_sl2_updated(inst, "SHORT", current_sl2, new_sl, window)
                lvl["sl2_short"]["sl"] = new_sl
                lvl["sl2_short"]["a"] = pt_a
                lvl["sl2_short"]["b"] = pt_b
                _set_state(inst, "levels", lvl)
            else:
                logger.info(f"{inst}: SHORT SL2 unchanged (new={new_sl:.2f} >= current={current_sl2:.2f}) - locked in")
                tg.send_sl2_locked(inst, "SHORT", current_sl2)

    except Exception as e:
        logger.error(f"Trailing SL2 update failed for {inst}: {e}")


def _update_trailing_sl1(inst: str, state: dict, direction: str, auto: bool):
    """
    Recompute SL1 using the rolling 2-day window (D-1, D-2).
    """
    try:
        from .data_fetcher import fetch_instrument_data
        data = fetch_instrument_data(inst)
        if not data or len(data["candles"]) < 2:
            return

        two = data["candles"][:2]
        h2 = max(d["high"] for d in two)
        l2 = min(d["low"]  for d in two)
        lvl = state.get("levels", {})
        e_l = state.get("long_entry_price") or lvl.get("e_l", 0)
        e_s = state.get("short_entry_price") or lvl.get("e_s", 0)

        current_sl1 = lvl.get(
            "sl1_long" if direction == "long" else "sl1_short", {}
        ).get("sl", 0)

        if direction == "long":
            pt_a = rt(e_l * 0.96)
            pt_b = rt(l2 * 0.996)
            new_sl = max(pt_a, pt_b)
            if new_sl > current_sl1:
                logger.info(f"{inst}: LONG SL1 trailing UP {current_sl1:.2f} -> {new_sl:.2f} (L2={l2})")
                tg.send(f"📈 <b>{inst} SL1 Trailing UP</b>\n"
                        f"Phase 1 SL updated: <b>{current_sl1:,.2f} -> {new_sl:,.2f}</b>")
                lvl["sl1_long"]["sl"] = new_sl
                lvl["sl1_long"]["a"] = pt_a
                lvl["sl1_long"]["b"] = pt_b
                _set_state(inst, "levels", lvl)
        else:
            pt_a = rt(e_s * 1.04)
            pt_b = rt(h2 * 1.004)
            new_sl = min(pt_a, pt_b)
            if new_sl < current_sl1:
                logger.info(f"{inst}: SHORT SL1 trailing DOWN {current_sl1:.2f} -> {new_sl:.2f} (H2={h2})")
                tg.send(f"📉 <b>{inst} SL1 Trailing DOWN</b>\n"
                        f"Phase 1 SL updated: <b>{current_sl1:,.2f} -> {new_sl:,.2f}</b>")
                lvl["sl1_short"]["sl"] = new_sl
                lvl["sl1_short"]["a"] = pt_a
                lvl["sl1_short"]["b"] = pt_b
                _set_state(inst, "levels", lvl)

    except Exception as e:
        logger.error(f"Trailing SL1 update failed for {inst}: {e}")


# ─── Scheduler setup ─────────────────────────────────────────────────────────

def start_scheduler():
    sched = BackgroundScheduler(timezone=IST)
    
    # 1. Fetch Data - 08:05 AM (Mon-Fri)
    sched.add_job(_fetch_and_broadcast, "cron", day_of_week="mon-fri", hour=8, minute=5, args=[False], id="morning_fetch")
    
    # 2. Morning Briefing - 08:30 AM (Mon-Fri)
    sched.add_job(_fetch_and_broadcast, "cron", day_of_week="mon-fri", hour=8, minute=30, args=[True], id="morning_broadcast")

    # 8:50 AM Mon–Fri: re-verify
    sched.add_job(_reverify, "cron",
                  day_of_week="mon-fri", hour=8, minute=50, id="reverify")

    # --- Morning Session (9:00 AM) Mon–Fri ---
    sched.add_job(_place_target_orders, "cron",
                  day_of_week="mon-fri", hour=9, minute=0, id="morning_target")
    sched.add_job(_place_entry_and_sl_orders, "cron",
                  day_of_week="mon-fri", hour=9, minute=10, id="morning_entry")
    sched.add_job(_handle_gap_recovery, "cron",
                  day_of_week="mon-fri", hour=9, minute=15, id="morning_gap")

    # --- Evening Session (5:00 PM) Mon–Fri ---
    sched.add_job(_place_target_orders, "cron",
                  day_of_week="mon-fri", hour=17, minute=0, id="evening_target")
    sched.add_job(_place_entry_and_sl_orders, "cron",
                  day_of_week="mon-fri", hour=17, minute=10, id="evening_entry")
    sched.add_job(_handle_gap_recovery, "cron",
                  day_of_week="mon-fri", hour=17, minute=15, id="evening_gap")

    sched.start()
    logger.info("Natural Gas Strategy scheduler started (Morning 9:00 | Evening 17:00 support)")
    return sched


def fetch_now(broadcast=None):
    """Manually trigger data fetch — callable from API endpoint."""
    _fetch_and_broadcast(broadcast=broadcast)
