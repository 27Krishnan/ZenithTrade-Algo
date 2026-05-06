import os
import csv
from datetime import datetime
from loguru import logger

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "nse_ohlc")

def get_nse_ohlc_from_csv(symbol: str, n_days: int = 10, before_date: str | None = None, expiry_date=None) -> list[dict]:
    """
    Reads OHLC data from the local NSE CSV database (e.g. nifty_26may2026_ohlc.csv).
    Returns a list of dicts (newest first).
    - before_date: "YYYY-MM-DD" (exclusive)
    """
    base_name = symbol.lower()
    if expiry_date:
        exp_str = expiry_date.strftime("%d%b%Y").lower()
        file_path = os.path.join(DATA_DIR, f"{base_name}_{exp_str}_ohlc.csv")
    else:
        file_path = os.path.join(DATA_DIR, f"{base_name}_ohlc.csv")

    if not os.path.exists(file_path):
        logger.error(f"NSE CSV not found: {file_path}")
        return []

    try:
        candles = []
        limit_dt = datetime.strptime(before_date, "%Y-%m-%d").date() if before_date else None
        today_str = datetime.now().strftime("%d-%b-%Y")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Format: Date,Open,High,Low,Close
                try:
                    dt = datetime.strptime(row['Date'], "%d-%b-%Y")
                    date_obj = dt.date()
                    
                    if not before_date and row['Date'] == today_str:
                        continue
                        
                    if limit_dt and date_obj >= limit_dt:
                        continue
                        
                    date_iso = dt.strftime("%Y-%m-%d")
                    
                    candles.append({
                        "date": date_iso,
                        "open": float(row['Open']),
                        "high": float(row['High']),
                        "low": float(row['Low']),
                        "close": float(row['Close']),
                    })
                except Exception as e:
                    logger.warning(f"Error parsing row {row}: {e}")
                    continue
        
        candles.sort(key=lambda x: x['date'], reverse=True)
        return candles[:n_days]
        
    except Exception as e:
        logger.error(f"Error reading NSE CSV for {symbol}: {e}")
        return []
