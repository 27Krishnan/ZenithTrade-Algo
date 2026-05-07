"""
Natural Gas Strategy — Telegram Alerts (Simplified)
"""
import sys, os, requests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loguru import logger
from config.settings import settings
from .database import get_setting

def _token() -> str: return get_setting("telegram_bot_token", "")
def _chat_id() -> str: return get_setting("telegram_chat_id", "")

def _is_enabled(key: str) -> bool:
    return get_setting(key, "true") == "true"

def send(message: str, setting_key: str = None) -> bool:
    if not settings.ENABLE_TELEGRAM_ALERTS:
        return False
    if setting_key and not _is_enabled(setting_key):
        return False
    token, chat_id = _token(), _chat_id()
    if not token or not chat_id: return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def _fmt(v):
    try: return f"{float(v):,.2f}"
    except: return str(v)

def send_morning_alert(levels_by_instrument: dict, live_states: dict | None = None):
    from datetime import datetime
    import pytz
    date = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%d-%b-%Y")
    header = f"📅 <b>{date}</b> — ZenithTrade Algo — NG Alert\n{'─'*25}"
    blocks = [header]
    for inst, lvl in levels_by_instrument.items():
        if not lvl: continue
        state = (live_states or {}).get(inst, {})
        l_st, s_st = state.get("long_state", "PENDING"), state.get("short_state", "PENDING")
        block = f"\n<b>⭐ {inst}</b>"
        if l_st in ("ACTIVE_P1", "ACTIVE_P2"):
            ep = state.get("long_entry_price", 0); ltp = state.get("ltp", 0); pnl = round(ltp - ep, 2) if (ltp and ep) else 0
            block += f"\n⬆️ BUY: 🔒 HOLDING | Entry: {_fmt(ep)} | P/L: <b>{'%+.2f' % pnl}</b>"
            if l_st == "ACTIVE_P1":
                block += f"\n   🎯 Tgt: {_fmt(lvl.get('t_l'))} | 🚨 SL1: {_fmt(lvl.get('sl1_long',{}).get('sl'))}"
            else:
                block += f"\n   ✅ T1 Hit | 🔄 SL2 (Trail): {_fmt(lvl.get('sl2_long',{}).get('sl'))}"
        else:
            block += f"\n⬆️ BUY: Entry: {_fmt(lvl.get('e_l'))} | Tgt: {_fmt(lvl.get('t_l'))} | SL: {_fmt(lvl.get('sl1_long',{}).get('sl'))}"
        
        if s_st in ("ACTIVE_P1", "ACTIVE_P2"):
            ep = state.get("short_entry_price", 0); ltp = state.get("ltp", 0); pnl = round(ep - ltp, 2) if (ltp and ep) else 0
            block += f"\n⬇️ SELL: 🔒 HOLDING | Entry: {_fmt(ep)} | P/L: <b>{'%+.2f' % pnl}</b>"
            if s_st == "ACTIVE_P1":
                block += f"\n   🎯 Tgt: {_fmt(lvl.get('t_s'))} | 🚨 SL1: {_fmt(lvl.get('sl1_short',{}).get('sl'))}"
            else:
                block += f"\n   ✅ T1 Hit | 🔄 SL2 (Trail): {_fmt(lvl.get('sl2_short',{}).get('sl'))}"
        else:
            block += f"\n⬇️ SELL: Entry: {_fmt(lvl.get('e_s'))} | Tgt: {_fmt(lvl.get('t_s'))} | SL: {_fmt(lvl.get('sl1_short',{}).get('sl'))}"
        blocks.append(block)
    send("\n".join(blocks), "tg_notify_morning")

def send_market_open(is_open: bool):
    status = "OPEN 🟢" if is_open else "CLOSED 🔴"
    send(f"📊 <b>MCX Market is {status}</b>", "tg_notify_market_open")

def send_entry_triggered(inst: str, direction: str, price: float, target: float, sl: float, symbol: str = ""):
    emoji = "🟢" if direction == "LONG" else "🔴"
    send(f"{emoji} <b>{inst} {direction} Entry @ {_fmt(price)}</b>\nTgt: {_fmt(target)} | SL: {_fmt(sl)}", "tg_notify_entry")

def send_sl_hit(inst: str, direction: str, sl_price: float, entry: float, pnl: float, lots: str = ""):
    send(f"🚨 <b>{inst} {direction} SL HIT @ {_fmt(sl_price)}</b>\nPnL: <b>{'%+.2f' % pnl} pts</b>", "tg_notify_exit")

def send_lot1_target_hit(inst: str, direction: str, target: float, lot1_pnl: float, sl2: float):
    send(f"🎯 <b>{inst} {direction} Target @ {_fmt(target)}</b>\nPnL: <b>+{_fmt(lot1_pnl)} pts</b> | SL2: {_fmt(sl2)}", "tg_notify_exit")

def send_gap(inst: str, direction: str, ltp: float, entry_level: float):
    gtype = "UP" if direction == "LONG" else "DOWN"
    send(f"⚠️ <b>{inst} GAP {gtype} @ {_fmt(ltp)}</b>\nSkipped — Recalculating @ 9:15", "tg_notify_gap")

def send_gap_recovery(inst: str, long_recovered: bool, short_recovered: bool, lvl: dict):
    msg = f"🔄 <b>{inst} GAP RECOVERY</b>\n"
    if long_recovered: msg += f"⬆️ LONG Entry: {_fmt(lvl['e_l'])} | SL: {_fmt(lvl['sl1_long']['sl'])}\n"
    if short_recovered: msg += f"⬇️ SHORT Entry: {_fmt(lvl['e_s'])} | SL: {_fmt(lvl['sl1_short']['sl'])}\n"
    send(msg, "tg_notify_gap")

def send_rollover_warning(inst: str, days_left: int, expiry: str):
    send(f"⏳ <b>{inst} Rollover Intimation</b>\nExpiry in {days_left} working days ({expiry}).")

def send_sl2_updated(inst: str, direction: str, old_sl: float, new_sl: float, window: str = ""):
    send(f"🔄 <b>{inst} {direction} SL Trailed</b>\nNew SL: {_fmt(new_sl)}")

def send_sl2_locked(inst: str, direction: str, current_sl: float): pass
