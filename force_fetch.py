"""Force fetch today's OHLC data for GOLD and GOLDM from Angel One API."""
import sys, os
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

os.chdir(project_root)

from gold_strategy.scheduler import fetch_now
from loguru import logger

print("=== Force fetching Gold strategy data ===")
try:
    fetch_now()
    print("=== Fetch complete ===")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()

# Now verify what got stored
import sqlite3, json
db_path = os.path.join(project_root, 'gold_strategy', 'gold_strategy.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

for inst in ['GOLD', 'GOLDM']:
    cursor.execute('SELECT levels_json, h4, l4 FROM daily_state WHERE instrument = ? AND date = ?',
                   (inst, '2026-04-28'))
    r = cursor.fetchone()
    if r:
        data = json.loads(r[0])
        raw = data.get('raw_days', [])
        print(f'\n=== {inst} after fetch ===')
        print(f'  h4={r[1]}, l4={r[2]}')
        for d in raw[:6]:
            print(f'    Date={d["date"]}  High={d["high"]}  Low={d["low"]}')
conn.close()
