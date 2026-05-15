import requests
import json
import time

BASE = "http://34.70.33.149:8000"

injections = [
    {"slug": "silver", "instrument": "SILVER", "state": {
        "long_state": "ACTIVE_P2", "long_entry_price": 296159.95, "long_entry_date": "2026-05-13",
        "long_lot1_closed": True, "long_pnl": 177696.0, "short_state": "PENDING",
        "levels_json": json.dumps({
            "e_l": 296159.95, "e_s": 254416.35, "t_l": 302083.15, "t_s": 249328.0,
            "sl1_long":  {"a": 290236.75, "b": 258115.9, "sl": 290236.75},
            "sl1_short": {"a": 259504.7,  "b": 283094.3, "sl": 259504.7},
            "sl2_long":  {"a": 290236.75, "b": 258115.9, "sl": 290236.75},
            "sl2_short": {"a": 259504.7,  "b": 283094.3, "sl": 259504.7},
            "lookback_count": 4})}},
    {"slug": "silver", "instrument": "SILVERM", "state": {
        "long_state": "ACTIVE_P2", "long_entry_price": 297747.85, "long_entry_date": "2026-05-13",
        "long_lot1_closed": True, "long_pnl": 29774.75, "short_state": "PENDING",
        "levels_json": json.dumps({
            "e_l": 297747.85, "e_s": 257304.85, "t_l": 303702.80, "t_s": 252158.75,
            "sl1_long":  {"a": 291792.9, "b": 260684.8, "sl": 291792.9},
            "sl1_short": {"a": 262450.95, "b": 284741.3, "sl": 262450.95},
            "sl2_long":  {"a": 291792.9, "b": 260684.8, "sl": 291792.9},
            "sl2_short": {"a": 262450.95, "b": 284741.3, "sl": 262450.95},
            "lookback_count": 4})}},
    {"slug": "silver", "instrument": "SILVERMIC", "state": {
        "long_state": "ACTIVE_P2", "long_entry_price": 297749.85, "long_entry_date": "2026-05-13",
        "long_lot1_closed": True, "long_pnl": 5955.0, "short_state": "PENDING",
        "levels_json": json.dumps({
            "e_l": 297749.85, "e_s": 257390.75, "t_l": 303704.85, "t_s": 252242.95,
            "sl1_long":  {"a": 291794.85, "b": 260786.7, "sl": 291794.85},
            "sl1_short": {"a": 262538.55, "b": 284821.4, "sl": 262538.55},
            "sl2_long":  {"a": 291794.85, "b": 260786.7, "sl": 291794.85},
            "sl2_short": {"a": 262538.55, "b": 284821.4, "sl": 262538.55},
            "lookback_count": 4})}}
]

print("Silver Injection v3 - Correct SL2")
print("=" * 45)

for o in injections:
    try:
        r = requests.post(f"{BASE}/api/strategy-hub/override-state", json=o, timeout=15)
        result = r.json() if r.text else {}
        status = "OK" if result.get("ok") else "FAIL"
        print(f"  [{status}] {o['instrument']}: {result.get('message', r.text[:80])}")
    except Exception as e:
        print(f"  [ERR] {o['instrument']}: {e}")

print()
print("Verification:")
try:
    d = requests.get(f"{BASE}/api/strategy-hub/overview", timeout=15).json()
    silver = next(s for s in d["strategies"] if s["slug"] == "silver")
    for inst, data in silver.get("live", {}).items():
        sl2 = data.get("levels", {}).get("sl2_long", {}).get("sl", "?")
        ls = data.get("long_state", "?")
        ep = data.get("long_entry_price", "?")
        print(f"  {inst}: state={ls} | entry={ep} | SL2={sl2}")
except Exception as e:
    print(f"  Verify error: {e}")
