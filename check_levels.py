import sqlite3, json

conn = sqlite3.connect(r'c:\Users\Admin\OneDrive\Swap Data\Papertrading\gold_strategy\gold_strategy.db')
cursor = conn.cursor()

for inst in ['GOLD', 'GOLDM']:
    cursor.execute('SELECT levels_json, h4, l4, h2, l2 FROM daily_state WHERE instrument = ? AND date = ?',
                   (inst, '2026-04-28'))
    r = cursor.fetchone()
    if r:
        data = json.loads(r[0])
        raw = data.get('raw_days', [])
        print(f'\n=== {inst} ===')
        print(f'  Stored: h4={r[1]}, l4={r[2]}, h2={r[3]}, l2={r[4]}')
        print(f'  raw_days:')
        for d in raw:
            print(f'    Date={d["date"]}  High={d["high"]}  Low={d["low"]}')
        # Calculate correct 4-day values from raw
        top4 = raw[:4]
        calc_h4 = max(d["high"] for d in top4) if top4 else None
        calc_l4 = min(d["low"]  for d in top4) if top4 else None
        calc_h2 = max(d["high"] for d in raw[:2]) if len(raw) >= 2 else None
        calc_l2 = min(d["low"]  for d in raw[:2]) if len(raw) >= 2 else None
        print(f'  CALCULATED from top-4 raw_days: H4={calc_h4}, L4={calc_l4}')
        print(f'  CALCULATED from top-2 raw_days: H2={calc_h2}, L2={calc_l2}')

conn.close()
