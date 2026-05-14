"""
Silver Manual Injection Script - CORRECT VERSION
================================================
Properly sets BOTH state AND levels so the monitor uses correct SL2.
Use this whenever manually injecting a trade from a backtest simulation.

Usage: python3 fix_silver_state_api.py
"""
import requests
import json
from datetime import date

BASE = "http://34.70.33.149:8000"

# ─── Backtest data from May 13 GAP RECOVERY ──────────────────────
# Source: simulate_silver_13th.py output (exact values)
INJECTIONS = [
    {
        "slug": "silver",
        "instrument": "SILVER",
        "state": {
            # Trade state
            "long_state": "ACTIVE_P2",
            "long_entry_price": 296159.95,
            "long_entry_date": "2026-05-13",
            "long_lot1_closed": True,
            "long_pnl": 177696.0,   # Lot-1 realized P&L
            "short_state": "PENDING",
            # CRITICAL: set correct SL2 in levels so monitor uses right value
            # Recalculated at day start: max(entry*0.98, L4*0.9988)
            # entry=296159.95*0.98=290236.75 | L4=258426*0.9988=258115.9 → SL2=290236.75
            "levels_json": json.dumps({
                "e_l": 296159.95,    # Gap-recovered entry
                "e_s": 254416.35,
                "t_l": 302083.15,    # Already hit
                "t_s": 249328.0,
                "sl1_long":  {"a": 290236.75, "b": 258115.9, "sl": 290236.75},
                "sl1_short": {"a": 259504.7,  "b": 283094.3, "sl": 259504.7},
                "sl2_long":  {"a": 290236.75, "b": 258115.9, "sl": 290236.75},
                "sl2_short": {"a": 259504.7,  "b": 283094.3, "sl": 259504.7},
                "lookback_count": 4,
            })
        }
    },
    {
        "slug": "silver",
        "instrument": "SILVERM",
        "state": {
            "long_state": "ACTIVE_P2",
            "long_entry_price": 297747.85,
            "long_entry_date": "2026-05-13",
            "long_lot1_closed": True,
            "long_pnl": 29774.75,
            "short_state": "PENDING",
            # SL2: max(297747.85*0.98, 260998*0.9988) = max(291792.9, 260684.8) = 291792.9
            "levels_json": json.dumps({
                "e_l": 297747.85,
                "e_s": 257304.85,
                "t_l": 303702.80,
                "t_s": 252158.75,
                "sl1_long":  {"a": 291792.9, "b": 260684.8, "sl": 291792.9},
                "sl1_short": {"a": 262450.95, "b": 284741.3, "sl": 262450.95},
                "sl2_long":  {"a": 291792.9, "b": 260684.8, "sl": 291792.9},
                "sl2_short": {"a": 262450.95, "b": 284741.3, "sl": 262450.95},
                "lookback_count": 4,
            })
        }
    },
    {
        "slug": "silver",
        "instrument": "SILVERMIC",
        "state": {
            "long_state": "ACTIVE_P2",
            "long_entry_price": 297749.85,
            "long_entry_date": "2026-05-13",
            "long_lot1_closed": True,
            "long_pnl": 5955.0,
            "short_state": "PENDING",
            # SL2: max(297749.85*0.98, 261100*0.9988) = max(291794.85, 260786.7) = 291794.85
            "levels_json": json.dumps({
                "e_l": 297749.85,
                "e_s": 257390.75,
                "t_l": 303704.85,
                "t_s": 252242.95,
                "sl1_long":  {"a": 291794.85, "b": 260786.7, "sl": 291794.85},
                "sl1_short": {"a": 262538.55, "b": 284821.4, "sl": 262538.55},
                "sl2_long":  {"a": 291794.85, "b": 260786.7, "sl": 291794.85},
                "sl2_short": {"a": 262538.55, "b": 284821.4, "sl": 262538.55},
                "lookback_count": 4,
            })
        }
    }
]

print(f"Silver Manual Injection — {date.today()}")
print("=" * 55)
for o in INJECTIONS:
    r = requests.post(f"{BASE}/api/strategy-hub/override-state", json=o)
    result = r.json()
    status = "OK" if result.get("ok") else "FAIL"
    print(f"  [{status}] {o['instrument']}: {result.get('message', r.text)}")

print()
print("Verifying live state...")
d = requests.get(f"{BASE}/api/strategy-hub/overview").json()
silver = next(s for s in d["strategies"] if s["slug"] == "silver")
for inst, data in silver.get("live", {}).items():
    sl2 = data.get("levels", {}).get("sl2_long", {}).get("sl", "?")
    print(f"  {inst}: long_state={data.get('long_state')} | entry={data.get('long_entry_price')} | SL2={sl2}")
