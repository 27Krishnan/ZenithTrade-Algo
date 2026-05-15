import requests
import json
from datetime import date

BASE_URL = "http://localhost:8000"
# OR use live IP if running locally: BASE_URL = "http://34.70.33.149:8000"

def reset_to_pending(slug, instrument):
    payload = {
        "slug": slug,
        "instrument": instrument,
        "state": {
            "long_state": "PENDING",
            "short_state": "PENDING",
            "long_entry_price": None,
            "long_entry_date": None,
            "long_lot1_closed": False,
            "long_pnl": 0,
            "short_entry_price": None,
            "short_entry_date": None,
            "short_lot1_closed": False,
            "short_pnl": 0
        }
    }
    try:
        r = requests.post(f"{BASE_URL}/api/strategy-hub/override-state", json=payload)
        res = r.json()
        print(f"[{slug.upper()}] {instrument} Reset: {res.get('message', 'Success')}")
    except Exception as e:
        print(f"Error resetting {instrument}: {e}")

if __name__ == "__main__":
    print(f"Resetting corrupted zombie states for {date.today()} to PENDING...")
    print("-" * 50)
    
    # Reset Gold
    reset_to_pending("gold", "GOLD")
    reset_to_pending("gold", "GOLDM")
    
    # Reset Silver
    reset_to_pending("silver", "SILVER")
    reset_to_pending("silver", "SILVERM")
    reset_to_pending("silver", "SILVERMIC")
    
    print("-" * 50)
    print("Done. Dashboard should now show fresh entries.")
