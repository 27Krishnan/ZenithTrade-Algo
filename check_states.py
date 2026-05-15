import requests

BASE = 'http://34.70.33.149:8000'
try:
    d = requests.get(f'{BASE}/api/strategy-hub/overview', timeout=15).json()
    for s in d['strategies']:
        if s['slug'] in ('gold', 'silver', 'natural_gas'):
            print(f"=== {s['slug'].upper()} ===")
            for inst, data in s.get('live', {}).items():
                ls = data.get('long_state', '?')
                ss = data.get('short_state', '?')
                lep = data.get('long_entry_price', '?')
                sep = data.get('short_entry_price', '?')
                print(f"{inst}: long={ls} (entry={lep}) | short={ss} (entry={sep})")
except Exception as e:
    print(f'Error: {e}')
