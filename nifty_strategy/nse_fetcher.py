import requests
import pandas as pd
from datetime import datetime
from loguru import logger
import time
import os

class NSEFetcher:
    """
    NSE Data Fetcher with Local CSV Fallback (Fixed Case Sensitivity).
    """
    BASE_URL = "https://www.nseindia.com/api/historical/fo/derivatives"
    LOCAL_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "nifty_nse_data.csv")
    
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/get-quote/derivatives/NIFTY/NIFTY%2050",
            "X-Requested-With": "XMLHttpRequest"
        }

    def _init_session(self):
        try:
            self.session.get("https://www.nseindia.com", headers=self.headers, timeout=10)
            time.sleep(1)
        except Exception:
            pass

    def fetch_nifty_futures(self, expiry_date="26-May-2026"):
        """
        Fetches NIFTY Futures data. Tries local CSV first for accuracy.
        """
        try:
            if os.path.exists(self.LOCAL_CSV):
                df = pd.read_csv(self.LOCAL_CSV)
                # Standardize column names to lowercase
                df.columns = [c.lower() for c in df.columns]
                
                data = df.to_dict('records')
                # Parse dates consistently
                for row in data:
                    dstr = row['date']
                    try:
                        # Try "04-May-2026" or "2026-05-04"
                        if '-' in dstr and len(dstr.split('-')[1]) == 3: # "04-May-2026"
                             row['_dt'] = datetime.strptime(dstr, "%d-%b-%Y")
                        else:
                             row['_dt'] = pd.to_datetime(dstr)
                    except Exception:
                        row['_dt'] = datetime.min
                
                # Sort newest first
                data.sort(key=lambda x: x['_dt'], reverse=True)
                logger.info(f"Loaded {len(data)} rows for NIFTY from Local CSV (Fixed).")
                return data
        except Exception as e:
            logger.error(f"Local CSV Load Error: {e}")

        # Fallback to API if CSV missing or fails
        return self._fetch_from_api(expiry_date)

    def _fetch_from_api(self, expiry_date):
        params = {
            "symbol": "NIFTY",
            "from": "01-01-2026",
            "to": datetime.now().strftime("%d-%m-%Y"),
            "expiryDate": expiry_date,
            "instrumentType": "FUTIDX"
        }
        try:
            self._init_session()
            response = self.session.get(self.BASE_URL, params=params, headers=self.headers, timeout=15)
            if response.status_code == 200:
                json_data = response.json()
                extracted = []
                for row in json_data.get('data', []):
                    if not row.get('FH_OPEN_PRICE'): continue
                    extracted.append({
                        "date": row.get('FH_TRADE_DATE'),
                        "open": float(row.get('FH_OPEN_PRICE', 0)),
                        "high": float(row.get('FH_HIGH_PRICE', 0)),
                        "low": float(row.get('FH_LOW_PRICE', 0)),
                        "close": float(row.get('FH_CLOSE_PRICE', 0))
                    })
                return extracted
        except Exception as e:
            logger.error(f"NSE API Error: {e}")
        return []

nse_fetcher = NSEFetcher()
