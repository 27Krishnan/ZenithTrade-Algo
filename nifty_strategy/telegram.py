"""
Gold Strategy — Telegram Alerts
=================================
Message types and their format:

1. MORNING ALERT (7:50 AM, fresh day — no trade yet):
   📅 Date | 4D High/Low | BUY: Entry/Target/SL | SELL: Entry/Target/SL

2. MORNING ALERT (next day, HOLDING a position):
   - If LONG is holding: DON'T show Long entry again.
     Show: Long → original entry (entered), fixed target, updated SL
     Show: Short → full entry/target/SL (not yet triggered)
   - If SHORT is holding: Same logic reversed

3. ENTRY TRIGGERED (9:10 AM, live):
   🟢 GOLD LONG ENTRY TRIGGERED @ 1,55,686.60

4. SL HIT:
   🚨 GOLD LONG STOP LOSS HIT @ 1,53,351.30

5. TARGET HIT (Lot-1):
   🎯 GOLD LONG LOT-1 TARGET HIT @ 1,58,021.90 | Lot-2 trailing SL: 1,53,351.30

6. SL2 UPDATED (next-day trailing, at 9:10 AM):
   🔄 GOLD LONG SL UPDATED | Old: 1,53,351.30 → New: 1,54,200.00
"""
import sys, os, requests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loguru import logger
from config.settings import settings
from .database import get_setting


# ─── Core send ───────────────────────────────────────────────────────────────

def _token() -> str:
    t = get_setting("telegram_bot_token", "")
    if not t or "7657983245" in t:
        return settings.TELEGRAM_BOT_TOKEN
    return t

def _chat_id() -> str:
    c = get_setting("telegram_chat_id", "")
    if not c or "-1002639599677" in c:
        return settings.TELEGRAM_CHAT_ID
    return c

def send(message: str) -> bool:
    if not settings.ENABLE_TELEGRAM_ALERTS:
        return False
    token   = _token()
    chat_id = _chat_id()
    if not token or not chat_id:
        logger.debug("Telegram not configured — skipping")
        return False
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML",
        }, timeout=10)
        if not resp.ok:
            logger.warning(f"Telegram send failed: {resp.text}")
        return resp.ok
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False

def send_msg(message: str) -> bool:
    """Wrapper for backward compatibility or direct text messages."""
    return send(message)


# ─── Message builders ─────────────────────────────────────────────────────────

def _fmt(v) -> str:
    """Format price with commas, 2 decimal places."""
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


def _morning_fresh_block(inst: str, lvl: dict) -> str:
    """Block for an instrument with no active positions — full BUY+SELL details."""
    sym    = lvl.get("trading_symbol", inst)
    h3     = _fmt(lvl.get("h3", lvl.get("h4", 0)))
    l3     = _fmt(lvl.get("l3", lvl.get("l4", 0)))
    e_l    = _fmt(lvl.get("e_l", 0))
    t_l    = _fmt(lvl.get("t_l", 0))
    sl1_l  = _fmt(lvl.get("sl1_long",  {}).get("sl", 0))
    e_s    = _fmt(lvl.get("e_s", 0))
    t_s    = _fmt(lvl.get("t_s", 0))
    sl1_s  = _fmt(lvl.get("sl1_short", {}).get("sl", 0))

    return (
        f"\n<b>⭐ {inst}</b>  <code>{sym}</code>\n"
        f"2-Day High: <b>{h3}</b>  |  2-Day Low: <b>{l3}</b>\n"
        f"\n"
        f"⬆️ <b>BUY</b>\n"
        f"  Entry:  <code>{e_l}</code>\n"
        f"  Target: <code>{t_l}</code>  (Lot-1, fixed)\n"
        f"  SL:     <code>{sl1_l}</code>\n"
        f"\n"
        f"⬇️ <b>SELL</b>\n"
        f"  Entry:  <code>{e_s}</code>\n"
        f"  Target: <code>{t_s}</code>  (Lot-1, fixed)\n"
        f"  SL:     <code>{sl1_s}</code>"
    )


def _morning_holding_block(inst: str, lvl: dict, live_state: dict) -> str:
    """
    Block for an instrument where one side is already holding.
    - Holding side: show Entry (already executed), Target (fixed), SL (current/updated)
    - Other side: show full Entry/Target/SL as usual
    - Key rule: DO NOT show entry price again for the holding side as a 'new entry'
    """
    sym      = lvl.get("trading_symbol", inst)
    h3       = _fmt(lvl.get("h3", lvl.get("h4", 0)))
    l3       = _fmt(lvl.get("l3", lvl.get("l4", 0)))
    l_state  = live_state.get("long_state",  "PENDING")
    s_state  = live_state.get("short_state", "PENDING")

    lines = [
        f"\n<b>⭐ {inst}</b>  <code>{sym}</code>",
        f"2-Day High: <b>{h3}</b>  |  2-Day Low: <b>{l3}</b>",
        "",
    ]

    # ── LONG side ─────────────────────────────────────────────────────────
    if l_state in ("ACTIVE_P1", "ACTIVE_P2"):
        # Already holding — don't show entry as a new signal
        entry_p = _fmt(live_state.get("long_entry_price", 0))
        target  = _fmt(lvl.get("t_l", 0))
        # Use current SL2 if Phase 2, else SL1
        if l_state == "ACTIVE_P2":
            current_sl = _fmt(lvl.get("sl2_long", {}).get("sl", 0))
            sl_label   = "SL (trailing)"
            lot_note   = "Lot-2 only"
        else:
            current_sl = _fmt(lvl.get("sl1_long", {}).get("sl", 0))
            sl_label   = "SL"
            lot_note   = "2 lots"

        # Calculate running P/L
        ltp = live_state.get("ltp", 0)
        entry_p_val = live_state.get("long_entry_price", 0)
        running_pnl = round(ltp - entry_p_val, 2) if (ltp and entry_p_val) else 0
        pnl_str = f"<b>{'%+.2f' % running_pnl}</b> pts"

        lines += [
            f"⬆️ <b>BUY</b>  🔒 <i>HOLDING ({lot_note})</i>",
            f"  Entry:  <code>{entry_p}</code>  ✅ entered",
        ]
        if l_state == "ACTIVE_P1":
            lines.append(f"  Target: <code>{target}</code>  (fixed)")
        else:
            lines.append(f"  ✅ Lot-1 Target Hit")
            
        lines += [
            f"  {sl_label}: <code>{current_sl}</code>",
            f"  Running P/L: {pnl_str}",
            "",
        ]
    else:
        # Not yet triggered — show as fresh signal
        e_l   = _fmt(lvl.get("e_l", 0))
        t_l   = _fmt(lvl.get("t_l", 0))
        sl1_l = _fmt(lvl.get("sl1_long", {}).get("sl", 0))
        lines += [
            f"⬆️ <b>BUY</b>",
            f"  Entry:  <code>{e_l}</code>",
            f"  Target: <code>{t_l}</code>  (Lot-1, fixed)",
            f"  SL:     <code>{sl1_l}</code>",
            "",
        ]

    # ── SHORT side ──────────────────────────────────────────────────────────
    if s_state in ("ACTIVE_P1", "ACTIVE_P2"):
        entry_p = _fmt(live_state.get("short_entry_price", 0))
        target  = _fmt(lvl.get("t_s", 0))
        if s_state == "ACTIVE_P2":
            current_sl = _fmt(lvl.get("sl2_short", {}).get("sl", 0))
            sl_label   = "SL (trailing)"
            lot_note   = "Lot-2 only"
        else:
            current_sl = _fmt(lvl.get("sl1_short", {}).get("sl", 0))
            sl_label   = "SL"
            lot_note   = "2 lots"

        # Calculate running P/L
        ltp = live_state.get("ltp", 0)
        entry_p_val = live_state.get("short_entry_price", 0)
        running_pnl = round(entry_p_val - ltp, 2) if (ltp and entry_p_val) else 0
        pnl_str = f"<b>{'%+.2f' % running_pnl}</b> pts"

        lines += [
            f"⬇️ <b>SELL</b>  🔒 <i>HOLDING ({lot_note})</i>",
            f"  Entry:  <code>{entry_p}</code>  ✅ entered",
        ]
        if s_state == "ACTIVE_P1":
            lines.append(f"  Target: <code>{target}</code>  (fixed)")
        else:
            lines.append(f"  ✅ Lot-1 Target Hit")

        lines += [
            f"  {sl_label}: <code>{current_sl}</code>",
            f"  Running P/L: {pnl_str}"
        ]
    else:
        e_s   = _fmt(lvl.get("e_s", 0))
        t_s   = _fmt(lvl.get("t_s", 0))
        sl1_s = _fmt(lvl.get("sl1_short", {}).get("sl", 0))
        lines += [
            f"⬇️ <b>SELL</b>",
            f"  Entry:  <code>{e_s}</code>",
            f"  Target: <code>{t_s}</code>  (Lot-1, fixed)",
            f"  SL:     <code>{sl1_s}</code>",
        ]

    return "\n".join(lines)


# ─── Public API ──────────────────────────────────────────────────────────────

def send_morning_alert(levels_by_instrument: dict, live_states: dict | None = None):
    """
    Send the morning briefing.

    levels_by_instrument: {inst: levels_dict}  (from calculator.to_dict())
    live_states:          {inst: live_state_dict}  — if provided, shows holding context
    """
    from datetime import datetime
    import pytz
    IST  = pytz.timezone("Asia/Kolkata")
    date = datetime.now(IST).strftime("%d-%b-%Y")

    header = f"📅 <b>{date}</b> — ZenithTrade Algo — Nifty Alert\n{'─'*30}"
    blocks = [header]

    for inst, lvl in levels_by_instrument.items():
        if not lvl:
            blocks.append(f"\n<b>{inst}</b> — ⚠️ No data")
            continue

        # Check if there's a holding position
        state = (live_states or {}).get(inst, {})
        l_st  = state.get("long_state",  "PENDING")
        s_st  = state.get("short_state", "PENDING")
        is_holding = l_st in ("ACTIVE_P1", "ACTIVE_P2") or s_st in ("ACTIVE_P1", "ACTIVE_P2")

        if is_holding and live_states:
            blocks.append(_morning_holding_block(inst, lvl, state))
        else:
            blocks.append(_morning_fresh_block(inst, lvl))

    send("\n".join(blocks))


def send_entry_triggered(instrument: str, direction: str, price: float,
                         target: float, sl: float, symbol: str = ""):
    """Entry trigger fired at 9:10 AM."""
    emoji = "🟢" if direction == "LONG" else "🔴"
    arrow = "⬆️" if direction == "LONG" else "⬇️"
    side  = "BUY" if direction == "LONG" else "SELL"
    msg = (
        f"{emoji} <b>{instrument} {direction} ENTRY TRIGGERED</b>\n"
        f"{arrow} <b>{side}</b>  <code>{symbol}</code>\n"
        f"  Entry:  <code>{_fmt(price)}</code>\n"
        f"  Target: <code>{_fmt(target)}</code>  (Lot-1)\n"
        f"  SL:     <code>{_fmt(sl)}</code>  (both lots)"
    )
    send(msg)


def send_sl_hit(instrument: str, direction: str, sl_price: float,
                entry: float, pnl: float, lots: str = "both lots"):
    """SL was hit — position closed."""
    msg = (
        f"🚨 <b>{instrument} {direction} STOP LOSS HIT</b>\n"
        f"  SL @ <code>{_fmt(sl_price)}</code>  ({lots} closed)\n"
        f"  Entry was: <code>{_fmt(entry)}</code>\n"
        f"  PnL: <b>{'%+.2f' % pnl} pts</b>"
    )
    send(msg)


def send_lot1_target_hit(instrument: str, direction: str,
                         target: float, lot1_pnl: float, sl2: float):
    """Lot-1 target hit — Lot-2 continues with trailing SL2."""
    emoji = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"🎯 <b>{instrument} {direction} LOT-1 TARGET HIT</b>\n"
        f"  {emoji} Lot-1 closed @ <code>{_fmt(target)}</code>  ✅\n"
        f"  Lot-1 PnL: <b>+{_fmt(lot1_pnl)} pts</b>\n"
        f"  Lot-2 continues — trailing SL: <code>{_fmt(sl2)}</code>"
    )
    send(msg)


def send_sl2_updated(instrument: str, direction: str,
                     old_sl: float, new_sl: float, window: str = ""):
    """Trailing SL2 updated at 9:10 AM next day."""
    arrow = "↑" if direction == "LONG" else "↓"
    msg = (
        f"🔄 <b>{instrument} {direction} SL UPDATED</b>  @9:10 AM\n"
        f"  Old SL: <code>{_fmt(old_sl)}</code>\n"
        f"  New SL: <code>{_fmt(new_sl)}</code>  {arrow} trailing\n"
    )
    if window:
        msg += f"  <i>({window})</i>"
    send(msg)


def send_sl2_locked(instrument: str, direction: str, current_sl: float):
    """SL2 recalculated but kept same direction (locked)."""
    msg = (
        f"🔒 <b>{instrument} {direction} SL LOCKED</b>  @9:10 AM\n"
        f"  SL stays @ <code>{_fmt(current_sl)}</code>  (unchanged)"
    )
    send(msg)


def send_gap(instrument: str, direction: str, ltp: float, entry_level: float):
    """Gap detected — entry skipped."""
    gtype = "UP" if direction == "LONG" else "DOWN"
    msg = (
        f"⚠️ <b>{instrument} GAP {gtype} DETECTED</b>\n"
        f"  LTP <code>{_fmt(ltp)}</code> crossed entry level <code>{_fmt(entry_level)}</code>\n"
        f"  {direction} entry SKIPPED"
    )
    send(msg)


# Legacy alias (called from monitor.py)
def send_trade_alert(instrument: str, direction: str, action: str,
                     price: float, reason: str = ""):
    """Backward-compatible wrapper."""
    if "ENTRY" in action.upper():
        emoji = "🟢" if direction == "LONG" else "🔴"
        send(f"{emoji} <b>{instrument} {direction} {action}</b>\nPrice: <code>{_fmt(price)}</code>")
    elif "EXIT" in action.upper() or "SL" in action.upper():
        send(f"🚨 <b>{instrument} {direction} {action}</b>\nPrice: <code>{_fmt(price)}</code>")
    else:
        send(f"📌 <b>{instrument} {direction} {action}</b>\nPrice: <code>{_fmt(price)}</code>")
