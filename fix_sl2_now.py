"""
fix_sl2_now.py
==============
One-time fix: force-recalculate SL2 for all active Silver/Gold trades using
the ENTRY-DATE-based 4-day candle window, bypassing the ratchet direction check.

Run once while the server is running:
    .\\venv_trading\\Scripts\\python.exe fix_sl2_now.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Bootstrap DB + API ────────────────────────────────────────────────────────
from database.db import init_db
init_db()

from data.angel_api import angel_api
if not angel_api.connect():
    print("ERROR: Could not connect to Angel One. Check credentials.")
    sys.exit(1)
print(f"Angel One connected.")

def rt(val, tick=0.05):
    return round(round(val / tick) * tick, 2)


# ── Helper: fetch last N completed daily candles before a date ────────────────
def fetch_daily_before(token: str, exchange: str, before_date_str: str, n: int = 4):
    """Returns newest-first list of {date, high, low} for N days before before_date_str."""
    from datetime import datetime, timedelta
    target = datetime.strptime(before_date_str, "%Y-%m-%d")
    from_dt = (target - timedelta(days=45)).strftime("%Y-%m-%d 09:00")
    to_dt   = (target - timedelta(days=1)).strftime("%Y-%m-%d 23:59")
    raw = angel_api.get_candle_data(token=token, exchange=exchange,
                                    interval="ONE_DAY", from_date=from_dt, to_date=to_dt)
    if not raw:
        return []
    candles = []
    for c in raw:
        d = c[0][:10]
        if d >= before_date_str:
            continue
        candles.append({"date": d, "high": float(c[2]), "low": float(c[3])})
    candles.sort(key=lambda x: x["date"], reverse=True)
    return candles[:n]


# ── Helper: parse entry date string (e.g. "21 Apr 09:15" → "2026-04-21") ──────
def parse_entry_date(entry_date_str: str) -> str:
    """Convert stored entry_date like '21 Apr 09:15' to 'YYYY-MM-DD'."""
    if not entry_date_str:
        return None
    from datetime import datetime
    # Try "21 Apr 09:15"
    for fmt in ("%d %b %H:%M", "%d %b", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(entry_date_str.strip(), fmt)
            year = 2026  # current year
            return f"{year}-{dt.month:02d}-{dt.day:02d}"
        except ValueError:
            pass
    return None


# ── SILVER: fix SILVER, SILVERM, SILVERMIC ───────────────────────────────────
def fix_silver():
    from silver_strategy.monitor import _live as silver_live, _lock as silver_lock
    from silver_strategy.database import upsert_state
    from silver_strategy.data_fetcher import fetch_instrument_data

    INSTRUMENTS = ["SILVER", "SILVERM", "SILVERMIC"]

    for inst in INSTRUMENTS:
        with silver_lock:
            state = dict(silver_live.get(inst, {}))

        if not state:
            print(f"[SILVER] {inst}: no live state found, skipping.")
            continue

        long_st  = state.get("long_state", "PENDING")
        short_st = state.get("short_state", "PENDING")

        fixed_any = False

        # ── SHORT active ──────────────────────────────────────────────────────
        if short_st in ("ACTIVE_P1", "ACTIVE_P2"):
            e_s        = state.get("short_entry_price")
            entry_date = parse_entry_date(state.get("short_entry_date"))
            token      = state.get("token")
            if not e_s or not token:
                print(f"[SILVER] {inst} SHORT: missing entry_price or token, skip.")
                continue

            # Fetch 4 daily candles from the entry-date window
            if entry_date:
                print(f"[SILVER] {inst} SHORT: fetching 4-day window before {entry_date}...")
                candles = fetch_daily_before(token, "MCX", entry_date, n=4)
            else:
                # Fallback: use current data_fetcher (last 4 days)
                data = fetch_instrument_data(inst)
                candles = data["candles"][:4] if data else []

            if len(candles) < 4:
                print(f"[SILVER] {inst} SHORT: only {len(candles)} candles, skip.")
                continue

            h4     = max(d["high"] for d in candles)
            pt_a   = rt(e_s * 1.02)
            pt_b   = rt(h4 * 1.0012)
            new_sl = min(pt_a, pt_b)
            dates  = " / ".join(d["date"][5:] for d in candles)

            print(f"[SILVER] {inst} SHORT: H4={h4} | A={pt_a} | B={pt_b} | "
                  f"NEW_SL2={new_sl}  (window: {dates})")

            lvl = state.get("levels", {})
            phase_key = "sl2_short" if short_st == "ACTIVE_P2" else "sl1_short"

            # Force-update both sl2 and sl1 b-points
            for sk in ("sl2_short", "sl1_short"):
                if sk in lvl:
                    if sk == "sl2_short":
                        lvl[sk]["b"]  = pt_b
                        lvl[sk]["a"]  = pt_a
                        lvl[sk]["sl"] = new_sl
                    # sl1 uses H2 for b — leave it; only recalc sl2

            with silver_lock:
                silver_live[inst]["levels"] = lvl
                silver_live[inst]["h4"]     = h4

            upsert_state(inst, {
                "levels_json": json.dumps(lvl),
                "h4": h4,
            })
            print(f"[SILVER] {inst} SHORT SL2 updated → {new_sl}")
            fixed_any = True

        # ── LONG active ───────────────────────────────────────────────────────
        if long_st in ("ACTIVE_P1", "ACTIVE_P2"):
            e_l        = state.get("long_entry_price")
            entry_date = parse_entry_date(state.get("long_entry_date"))
            token      = state.get("token")
            if not e_l or not token:
                print(f"[SILVER] {inst} LONG: missing entry_price or token, skip.")
                continue

            if entry_date:
                print(f"[SILVER] {inst} LONG: fetching 4-day window before {entry_date}...")
                candles = fetch_daily_before(token, "MCX", entry_date, n=4)
            else:
                data = fetch_instrument_data(inst)
                candles = data["candles"][:4] if data else []

            if len(candles) < 4:
                print(f"[SILVER] {inst} LONG: only {len(candles)} candles, skip.")
                continue

            l4     = min(d["low"] for d in candles)
            pt_a   = rt(e_l * 0.98)
            pt_b   = rt(l4 * 0.9988)
            new_sl = max(pt_a, pt_b)
            dates  = " / ".join(d["date"][5:] for d in candles)

            print(f"[SILVER] {inst} LONG: L4={l4} | A={pt_a} | B={pt_b} | "
                  f"NEW_SL2={new_sl}  (window: {dates})")

            lvl = state.get("levels", {})
            if "sl2_long" in lvl:
                lvl["sl2_long"]["b"]  = pt_b
                lvl["sl2_long"]["a"]  = pt_a
                lvl["sl2_long"]["sl"] = new_sl

            with silver_lock:
                silver_live[inst]["levels"] = lvl
                silver_live[inst]["l4"]     = l4

            upsert_state(inst, {
                "levels_json": json.dumps(lvl),
                "l4": l4,
            })
            print(f"[SILVER] {inst} LONG SL2 updated → {new_sl}")
            fixed_any = True

        if not fixed_any:
            print(f"[SILVER] {inst}: no active trade (long={long_st}, short={short_st}), skip.")


# ── GOLD: fix GOLD, GOLDM ─────────────────────────────────────────────────────
def fix_gold():
    from gold_strategy.monitor import _live as gold_live, _lock as gold_lock
    from gold_strategy.database import upsert_state
    from gold_strategy.data_fetcher import fetch_instrument_data

    INSTRUMENTS = ["GOLD", "GOLDM"]

    for inst in INSTRUMENTS:
        with gold_lock:
            state = dict(gold_live.get(inst, {}))

        if not state:
            print(f"[GOLD] {inst}: no live state found, skipping.")
            continue

        long_st  = state.get("long_state", "PENDING")
        short_st = state.get("short_state", "PENDING")
        fixed_any = False

        # ── SHORT active ──────────────────────────────────────────────────────
        if short_st in ("ACTIVE_P1", "ACTIVE_P2"):
            e_s        = state.get("short_entry_price")
            entry_date = parse_entry_date(state.get("short_entry_date"))
            token      = state.get("token")
            if not e_s or not token:
                print(f"[GOLD] {inst} SHORT: missing entry_price or token, skip.")
                continue

            if entry_date:
                print(f"[GOLD] {inst} SHORT: fetching 4-day window before {entry_date}...")
                candles = fetch_daily_before(token, "MCX", entry_date, n=4)
            else:
                data = fetch_instrument_data(inst)
                candles = data["candles"][:4] if data else []

            if len(candles) < 4:
                print(f"[GOLD] {inst} SHORT: only {len(candles)} candles, skip.")
                continue

            h4     = max(d["high"] for d in candles)
            pt_a   = rt(e_s * 1.015)   # Gold uses 1.5% not 2%
            pt_b   = rt(h4 * 1.0012)
            new_sl = min(pt_a, pt_b)
            dates  = " / ".join(d["date"][5:] for d in candles)

            print(f"[GOLD] {inst} SHORT: H4={h4} | A={pt_a} | B={pt_b} | "
                  f"NEW_SL2={new_sl}  (window: {dates})")

            lvl = state.get("levels", {})
            if "sl2_short" in lvl:
                lvl["sl2_short"]["b"]  = pt_b
                lvl["sl2_short"]["a"]  = pt_a
                lvl["sl2_short"]["sl"] = new_sl

            with gold_lock:
                gold_live[inst]["levels"] = lvl
                gold_live[inst]["h4"]     = h4

            upsert_state(inst, {
                "levels_json": json.dumps(lvl),
                "h4": h4,
            })
            print(f"[GOLD] {inst} SHORT SL2 updated → {new_sl}")
            fixed_any = True

        # ── LONG active ───────────────────────────────────────────────────────
        if long_st in ("ACTIVE_P1", "ACTIVE_P2"):
            e_l        = state.get("long_entry_price")
            entry_date = parse_entry_date(state.get("long_entry_date"))
            token      = state.get("token")
            if not e_l or not token:
                print(f"[GOLD] {inst} LONG: missing entry_price or token, skip.")
                continue

            if entry_date:
                print(f"[GOLD] {inst} LONG: fetching 4-day window before {entry_date}...")
                candles = fetch_daily_before(token, "MCX", entry_date, n=4)
            else:
                data = fetch_instrument_data(inst)
                candles = data["candles"][:4] if data else []

            if len(candles) < 4:
                print(f"[GOLD] {inst} LONG: only {len(candles)} candles, skip.")
                continue

            l4     = min(d["low"] for d in candles)
            pt_a   = rt(e_l * 0.985)   # Gold uses 1.5% not 2%
            pt_b   = rt(l4 * 0.9988)
            new_sl = max(pt_a, pt_b)
            dates  = " / ".join(d["date"][5:] for d in candles)

            print(f"[GOLD] {inst} LONG: L4={l4} | A={pt_a} | B={pt_b} | "
                  f"NEW_SL2={new_sl}  (window: {dates})")

            lvl = state.get("levels", {})
            if "sl2_long" in lvl:
                lvl["sl2_long"]["b"]  = pt_b
                lvl["sl2_long"]["a"]  = pt_a
                lvl["sl2_long"]["sl"] = new_sl

            with gold_lock:
                gold_live[inst]["levels"] = lvl
                gold_live[inst]["l4"]     = l4

            upsert_state(inst, {
                "levels_json": json.dumps(lvl),
                "l4": l4,
            })
            print(f"[GOLD] {inst} LONG SL2 updated → {new_sl}")
            fixed_any = True

        if not fixed_any:
            print(f"[GOLD] {inst}: no active trade (long={long_st}, short={short_st}), skip.")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("SL2 Force-Fix Script")
    print("=" * 60)

    # NOTE: This script patches _live memory of the RUNNING server only if
    # run inside the same process. Since uvicorn runs separately, this script
    # will fix the DB values. The server will pick them up on next data fetch
    # (7:50 AM) OR you can use the /api/strategy/silver/fetch-now endpoint.
    # 
    # To also patch live memory immediately, use the API approach below.

    import requests

    BASE = "http://localhost:8000"

    print("\n--- Triggering Silver fetch-now via API ---")
    try:
        r = requests.post(f"{BASE}/api/strategy/silver/fetch-now", timeout=30)
        print(f"Silver fetch-now: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"Silver fetch-now failed: {e}")

    print("\n--- Triggering Gold fetch-now via API ---")
    try:
        r = requests.post(f"{BASE}/api/strategy/gold/fetch-now", timeout=30)
        print(f"Gold fetch-now: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"Gold fetch-now failed: {e}")

    print("\nDone. Refresh the dashboard to see updated values.")
    print("If values still wrong, check Strategy Center → Run Backtest → Sync to Live.")
