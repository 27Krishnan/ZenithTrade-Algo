import requests

BASE = "http://34.70.33.149:8000"

try:
    d = requests.get(f"{BASE}/api/strategy-hub/overview", timeout=15).json()
    silver = next(s for s in d["strategies"] if s["slug"] == "silver")
    print("=== SILVER LIVE STATE ===")
    for inst, data in silver.get("live", {}).items():
        ls = data.get("long_state", "?")
        ltp = data.get("ltp", "?")
        ep = data.get("long_entry_price", "?")
        sl2 = data.get("levels", {}).get("sl2_long", {}).get("sl", "?")
        print(f"{inst}: state={ls} | LTP={ltp} | entry={ep} | SL2={sl2}")
        if ltp and sl2 and isinstance(ltp, (int, float)) and isinstance(sl2, (int, float)):
            print(f"         LTP vs SL2: {'ABOVE SL2 - safe' if ltp > sl2 else 'BELOW SL2 - would trigger!'}")
except Exception as e:
    print(f"Error: {e}")
