import requests
import json

try:
    d = requests.get('http://34.70.33.149:8000/api/strategy-hub/history?strategy=silver', timeout=15).json()
    print(json.dumps(d[:10], indent=2))
except Exception as e:
    print('Error:', e)
