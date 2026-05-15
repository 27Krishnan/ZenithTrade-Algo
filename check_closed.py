import requests

BASE = 'http://34.70.33.149:8000'
try:
    d = requests.get(f'{BASE}/api/pnl/dashboard', timeout=15).json()
    recent = d.get('recent_closed_trades', [])
    print("=== RECENT CLOSED TRADES ===")
    for t in recent[:10]:
        print(f"ID={t['id']} | {t['symbol']} | {t['action']} | Entry={t['entry_price']} | Exit={t['exit_price']} | Reason={t['exit_reason']} | Time={t['exit_time']}")
except Exception as e:
    print(f'Error: {e}')
