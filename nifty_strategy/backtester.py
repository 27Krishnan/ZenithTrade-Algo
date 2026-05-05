"""
Nifty Strategy Backtester
========================
Simulates Nifty strategy logic with a 2-day lookback and 09:15-09:30 gap rules.
"""
from datetime import datetime, timedelta
from loguru import logger

from .calculator import NiftyLevels, rt
from data.angel_api import angel_api

LOTS = 2
MULTIPLIER = 65
MAX_SIMULATION_DAYS = 15


def _build_simulation(
    long_state,
    short_state,
    long_entry,
    short_entry,
    long_entry_date,
    short_entry_date,
    long_lot1_pnl,
    short_lot1_pnl,
    long_pnl,
    short_pnl,
    events,
    levels,
):
    return {
        "long_state": long_state,
        "short_state": short_state,
        "long_entry": long_entry,
        "short_entry": short_entry,
        "long_lot1_pnl": long_lot1_pnl,
        "short_lot1_pnl": short_lot1_pnl,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "long_entry_date": long_entry_date,
        "short_entry_date": short_entry_date,
        "events": events,
        "effective_levels": levels,
        "initial_levels": levels,
    }


def run_backtest(instrument: str, date_str: str):
    """
    Simulates the strategy for a specific day using 1-minute data.
    date_str: "YYYY-MM-DD"
    """
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        logger.info(f"Nifty Backtest: Starting for {instrument} on {date_str}")

        angel_api.connect()
        token_info = angel_api.get_current_future_symbol(instrument, "NFO", ref_date=target_date)
        if not token_info:
            return {"error": f"Could not find token for {instrument} on {date_str}"}

        token = token_info["token"]
        sym = token_info["symbol"]

        hist_df = angel_api.get_historical_data(token=token, exchange="NFO", interval="ONE_DAY", days=10)
        past_candles = hist_df[hist_df["date"] < date_str]

        if len(past_candles) < 2:
            logger.info(f"Nifty Backtest: Not enough data in {sym}, trying near-month fallback...")
            near_info = angel_api.get_current_future_symbol(
                instrument, "NFO", ref_date=target_date, allow_rollover=False
            )
            if near_info and near_info["token"] != token:
                token = near_info["token"]
                sym = near_info["symbol"]
                hist_df = angel_api.get_historical_data(token=token, exchange="NFO", interval="ONE_DAY", days=10)
                past_candles = hist_df[hist_df["date"] < date_str]

        if len(past_candles) < 2:
            return {"error": f"Not enough historical data before {date_str} even with near-month fallback."}

        past_candles = past_candles.head(2)
        raw_days = []
        for _, row in past_candles.iterrows():
            raw_days.append({
                "date": row["date"],
                "high": float(row["high"]),
                "low": float(row["low"])
            })

        lvls = NiftyLevels(instrument, sym, token, raw_days)
        levels = lvls.to_dict()

        intraday = angel_api.get_candle_data(token, "NFO", "ONE_MINUTE", f"{date_str} 09:15", f"{date_str} 15:30")
        if not intraday:
            events = [{"time": "09:00", "event": "No intraday data found for this date."}]
            simulation = _build_simulation(
                "PENDING", "PENDING", None, None, None, None, None, None, None, None, events, levels
            )
            return {
                "strategy": "NIFTY • MathZing",
                "instrument": instrument,
                "date": date_str,
                "levels": levels,
                "events": events,
                "total_pnl": 0,
                "status": "NO_DATA",
                "simulation": simulation,
            }

        events = []
        if len(raw_days) >= 2:
            events.append({"time": "09:00", "event": f"Levels Calculated using {raw_days[0]['date']} & {raw_days[1]['date']}"})
        else:
            events.append({"time": "09:00", "event": "Levels Calculated (Incomplete history)"})

        long_state = "PENDING"
        short_state = "PENDING"
        long_entry = None
        short_entry = None
        long_entry_date = None
        short_entry_date = None
        long_lot1_closed = False
        short_lot1_closed = False
        long_lot1_pnl = None
        short_lot1_pnl = None
        long_pnl = None
        short_pnl = None
        total_pnl = 0.0
        window_high = 0.0
        window_low = 999999.0
        last_close = None

        all_history = angel_api.get_historical_data(token, "NFO", "ONE_DAY", 30)
        if all_history is not None and not all_history.empty:
            all_history = all_history.sort_values(by="date", ascending=False)

        current_sim_date = target_date
        sim_day_count = 0

        while sim_day_count < MAX_SIMULATION_DAYS:
            date_iso = current_sim_date.strftime("%Y-%m-%d")
            cur_h2, cur_l2, win_str = 0.0, 999999.0, "No History"

            if all_history is not None:
                past_data = all_history[all_history["date"] < date_iso].head(2)
                if len(past_data) >= 2:
                    win_days = []
                    for _, row in past_data.iterrows():
                        win_days.append({
                            "date": row["date"],
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                        })
                    cur_h2 = max(win_days[0]["high"], win_days[1]["high"])
                    cur_l2 = min(win_days[0]["low"], win_days[1]["low"])
                    win_str = f"[{win_days[0]['date']} / {win_days[1]['date']}]"

            intraday = angel_api.get_candle_data(
                token, "NFO", "ONE_MINUTE", f"{date_iso} 09:00", f"{date_iso} 15:30"
            )
            if not intraday:
                if current_sim_date.date() >= datetime.now().date():
                    break
                current_sim_date += timedelta(days=1)
                sim_day_count += 1
                continue

            if sim_day_count > 0:
                if long_state == "ACTIVE_P1":
                    pt_a = rt(long_entry * 0.9875)
                    pt_b = rt(cur_l2 * 0.99875)
                    levels["sl1_long"]["a"] = pt_a
                    levels["sl1_long"]["b"] = pt_b
                    levels["sl1_long"]["sl"] = max(pt_a, pt_b)
                    events.append({
                        "time": f"{date_iso} 09:10",
                        "event": f"🔵 LONG (2 Lots) active from {long_entry} | SL1={levels['sl1_long']['sl']} | Window {win_str} | L2={cur_l2} | A={pt_a} B={pt_b} -> SL1={levels['sl1_long']['sl']}"
                    })

                if long_state == "ACTIVE_P2":
                    pt_a = long_entry
                    pt_b = rt(cur_l2 * 0.99875)
                    levels["sl2_long"]["a"] = pt_a
                    levels["sl2_long"]["b"] = pt_b
                    levels["sl2_long"]["sl"] = max(pt_a, pt_b)
                    events.append({
                        "time": f"{date_iso} 09:10",
                        "event": f"🔵 LONG (Lot-2) active from {long_entry} | SL2={levels['sl2_long']['sl']} | Window {win_str} | L2={cur_l2} | A={pt_a} B={pt_b} -> SL2={levels['sl2_long']['sl']}"
                    })

                if short_state == "ACTIVE_P1":
                    pt_a = rt(short_entry * 1.0125)
                    pt_b = rt(cur_h2 * 1.00125)
                    levels["sl1_short"]["a"] = pt_a
                    levels["sl1_short"]["b"] = pt_b
                    levels["sl1_short"]["sl"] = min(pt_a, pt_b)
                    events.append({
                        "time": f"{date_iso} 09:10",
                        "event": f"🔴 SHORT (2 Lots) active from {short_entry} | SL1={levels['sl1_short']['sl']} | Window {win_str} | H2={cur_h2} | A={pt_a} B={pt_b} -> SL1={levels['sl1_short']['sl']}"
                    })

                if short_state == "ACTIVE_P2":
                    pt_a = short_entry
                    pt_b = rt(cur_h2 * 1.00125)
                    levels["sl2_short"]["a"] = pt_a
                    levels["sl2_short"]["b"] = pt_b
                    levels["sl2_short"]["sl"] = min(pt_a, pt_b)
                    events.append({
                        "time": f"{date_iso} 09:10",
                        "event": f"🔴 SHORT (Lot-2) active from {short_entry} | SL2={levels['sl2_short']['sl']} | Window {win_str} | H2={cur_h2} | A={pt_a} B={pt_b} -> SL2={levels['sl2_short']['sl']}"
                    })

            for candle in intraday:
                if not candle or len(candle) < 5:
                    continue

                ts, o, h, l, c = candle[:5]
                try:
                    dt_str = ts.split("+")[0].replace("T", " ")
                    raw_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    if raw_dt.hour < 9:
                        raw_dt = raw_dt + timedelta(hours=5, minutes=30)
                    time_str = raw_dt.strftime("%H:%M")
                except Exception:
                    time_part = ts.split("T")[1] if "T" in ts else ts.split(" ")[1]
                    time_str = time_part[:5]

                ch = float(h)
                cl = float(l)
                ltp = float(c)
                last_close = ltp

                if sim_day_count == 0 and time_str == "09:15":
                    open_price = float(o)
                    if open_price >= (levels["e_l"] or 999999):
                        events.append({"time": f"{date_iso} 09:15", "event": f"⚠️ GAP UP at {open_price} | Window {win_str}. Waiting for 09:30."})
                        long_state = "GAP"
                    elif open_price <= (levels["e_s"] or 0):
                        events.append({"time": f"{date_iso} 09:15", "event": f"⚠️ GAP DOWN at {open_price} | Window {win_str}. Waiting for 09:30."})
                        short_state = "GAP"

                if "09:15" <= time_str <= "09:30":
                    window_high = max(window_high, ch)
                    window_low = min(window_low, cl)

                if time_str >= "09:30" and (long_state == "GAP" or short_state == "GAP"):
                    if long_state == "GAP":
                        new_e = rt(window_high * 1.00125)
                        pt_a = rt(new_e * 0.9875)
                        pt_b = rt(cur_l2 * 0.99875)
                        levels["e_l"] = new_e
                        levels["t_l"] = rt(new_e * 1.0125)
                        levels["sl1_long"]["a"] = pt_a
                        levels["sl1_long"]["b"] = pt_b
                        levels["sl1_long"]["sl"] = max(pt_a, pt_b)
                        levels["sl2_long"]["a"] = new_e
                        levels["sl2_long"]["b"] = pt_b
                        levels["sl2_long"]["sl"] = max(new_e, pt_b)
                        long_state = "PENDING"
                        events.append({"time": f"{date_iso} {time_str}", "event": f"🔄 GAP RECOVERY (LONG): New Entry L={new_e} | Target L={levels['t_l']} | SL1 L={levels['sl1_long']['sl']}"})
                    if short_state == "GAP":
                        new_e = rt(window_low * 0.99875)
                        pt_a = rt(new_e * 1.0125)
                        pt_b = rt(cur_h2 * 1.00125)
                        levels["e_s"] = new_e
                        levels["t_s"] = rt(new_e * 0.9875)
                        levels["sl1_short"]["a"] = pt_a
                        levels["sl1_short"]["b"] = pt_b
                        levels["sl1_short"]["sl"] = min(pt_a, pt_b)
                        levels["sl2_short"]["a"] = new_e
                        levels["sl2_short"]["b"] = pt_b
                        levels["sl2_short"]["sl"] = min(new_e, pt_b)
                        short_state = "PENDING"
                        events.append({"time": f"{date_iso} {time_str}", "event": f"🔄 GAP RECOVERY (SHORT): New Entry S={new_e} | Target S={levels['t_s']} | SL1 S={levels['sl1_short']['sl']}"})

                if long_state == "PENDING" and ch >= (levels["e_l"] or 999999):
                    long_state = "ACTIVE_P1"
                    long_entry = levels["e_l"]
                    long_entry_date = date_iso
                    events.append({"time": f"{date_iso} {time_str}", "event": f"🟢 LONG Entry (2 Lots) triggered @ {long_entry} | SL1={levels['sl1_long']['sl']} | Tgt={levels['t_l']}"})

                if short_state == "PENDING" and cl <= (levels["e_s"] or 0):
                    short_state = "ACTIVE_P1"
                    short_entry = levels["e_s"]
                    short_entry_date = date_iso
                    events.append({"time": f"{date_iso} {time_str}", "event": f"🔴 SHORT Entry (2 Lots) triggered @ {short_entry} | SL1={levels['sl1_short']['sl']} | Tgt={levels['t_s']}"})

                if long_state == "ACTIVE_P1":
                    cur_sl = levels["sl1_long"]["sl"]
                    cur_tgt = levels["t_l"]
                    if cl <= cur_sl:
                        long_pnl = round((cur_sl - long_entry) * LOTS * MULTIPLIER, 2)
                        total_pnl += long_pnl
                        events.append({"time": f"{date_iso} {time_str}", "event": f"📉 LONG SL HIT @ {cur_sl} | PnL: {long_pnl:+.2f} | Trade CLOSED"})
                        long_state = "CLOSED"
                        break
                    if ch >= cur_tgt and not long_lot1_closed:
                        long_lot1_closed = True
                        long_lot1_pnl = round((cur_tgt - long_entry) * MULTIPLIER, 2)
                        total_pnl += long_lot1_pnl
                        long_state = "ACTIVE_P2"
                        events.append({"time": f"{date_iso} {time_str}", "event": f"🎯 LONG LOT-1 TARGET HIT @ {cur_tgt} | PnL: {long_lot1_pnl:+.2f} | Lot-2 continues with Trailing SL"})

                if long_state == "ACTIVE_P2":
                    cur_sl = levels["sl2_long"]["sl"]
                    if cl <= cur_sl:
                        lot2_pnl = round((cur_sl - long_entry) * MULTIPLIER, 2)
                        long_pnl = round((long_lot1_pnl or 0) + lot2_pnl, 2)
                        total_pnl += lot2_pnl
                        events.append({"time": f"{date_iso} {time_str}", "event": f"🏁 LONG LOT-2 SL HIT @ {cur_sl} | PnL: {lot2_pnl:+.2f} | TOTAL: {long_pnl:+.2f} | Trade CLOSED"})
                        long_state = "CLOSED"
                        break

                if short_state == "ACTIVE_P1":
                    cur_sl = levels["sl1_short"]["sl"]
                    cur_tgt = levels["t_s"]
                    if ch >= cur_sl:
                        short_pnl = round((short_entry - cur_sl) * LOTS * MULTIPLIER, 2)
                        total_pnl += short_pnl
                        events.append({"time": f"{date_iso} {time_str}", "event": f"📉 SHORT SL HIT @ {cur_sl} | PnL: {short_pnl:+.2f} | Trade CLOSED"})
                        short_state = "CLOSED"
                        break
                    if cl <= cur_tgt and not short_lot1_closed:
                        short_lot1_closed = True
                        short_lot1_pnl = round((short_entry - cur_tgt) * MULTIPLIER, 2)
                        total_pnl += short_lot1_pnl
                        short_state = "ACTIVE_P2"
                        events.append({"time": f"{date_iso} {time_str}", "event": f"🎯 SHORT LOT-1 TARGET HIT @ {cur_tgt} | PnL: {short_lot1_pnl:+.2f} | Lot-2 continues with Trailing SL"})

                if short_state == "ACTIVE_P2":
                    cur_sl = levels["sl2_short"]["sl"]
                    if ch >= cur_sl:
                        lot2_pnl = round((short_entry - cur_sl) * MULTIPLIER, 2)
                        short_pnl = round((short_lot1_pnl or 0) + lot2_pnl, 2)
                        total_pnl += lot2_pnl
                        events.append({"time": f"{date_iso} {time_str}", "event": f"🏁 SHORT LOT-2 SL HIT @ {cur_sl} | PnL: {lot2_pnl:+.2f} | TOTAL: {short_pnl:+.2f} | Trade CLOSED"})
                        short_state = "CLOSED"
                        break

            if long_state == "CLOSED" or short_state == "CLOSED":
                break

            current_sim_date += timedelta(days=1)
            sim_day_count += 1

        if long_state == "ACTIVE_P1" and long_entry is not None and last_close is not None:
            long_pnl = round((last_close - long_entry) * LOTS * MULTIPLIER, 2)
        elif long_state == "ACTIVE_P2" and long_entry is not None and last_close is not None:
            lot2_pnl = round((last_close - long_entry) * MULTIPLIER, 2)
            long_pnl = round((long_lot1_pnl or 0) + lot2_pnl, 2)

        if short_state == "ACTIVE_P1" and short_entry is not None and last_close is not None:
            short_pnl = round((short_entry - last_close) * LOTS * MULTIPLIER, 2)
        elif short_state == "ACTIVE_P2" and short_entry is not None and last_close is not None:
            lot2_pnl = round((short_entry - last_close) * MULTIPLIER, 2)
            short_pnl = round((short_lot1_pnl or 0) + lot2_pnl, 2)

        simulation = _build_simulation(
            long_state,
            short_state,
            long_entry,
            short_entry,
            long_entry_date,
            short_entry_date,
            long_lot1_pnl,
            short_lot1_pnl,
            long_pnl,
            short_pnl,
            events,
            levels,
        )

        return {
            "strategy": "NIFTY • MathZing",
            "instrument": instrument,
            "trading_symbol": sym,
            "date": date_str,
            "levels": levels,
            "historical_data": raw_days,
            "events": events,
            "total_pnl": round(total_pnl, 2),
            "status": "COMPLETED",
            "simulation": simulation,
        }
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return {"error": str(e)}
