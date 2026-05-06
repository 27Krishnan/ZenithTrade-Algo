import os
import csv
from datetime import datetime
from loguru import logger

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mcx_ohlc")

def get_mcx_ohlc_from_csv(commodity: str, n_days: int = 10, before_date: str | None = None) -> list[dict]:
    """
    Reads OHLC data for a commodity from the local MCX CSV database.
    Returns a list of dicts (newest first).
    - before_date: "YYYY-MM-DD" (exclusive)
    """
    file_path = os.path.join(DATA_DIR, f"{commodity.lower()}_ohlc.csv")
    if not os.path.exists(file_path):
        logger.error(f"MCX CSV not found: {file_path}")
        return []

    try:
        candles = []
        limit_dt = datetime.strptime(before_date, "%Y-%m-%d").date() if before_date else None
        today_str = datetime.now().strftime("%d %b %Y")
        
        with open(file_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Format: Date,Open,High,Low,Close,Volume,OI
                try:
                    dt = datetime.strptime(row['Date'], "%d %b %Y")
                    date_obj = dt.date()
                    
                    # If we are in live mode (no before_date), exclude today's incomplete candle
                    if not before_date and row['Date'] == today_str:
                        continue
                        
                    # If we have a before_date, exclude anything on or after it
                    if limit_dt and date_obj >= limit_dt:
                        continue
                        
                    date_iso = dt.strftime("%Y-%m-%d")
                    
                    candles.append({
                        "date": date_iso,
                        "open": float(row['Open']),
                        "high": float(row['High']),
                        "low": float(row['Low']),
                        "close": float(row['Close']),
                        "volume": int(float(row['Volume'].replace(',', ''))),
                        "oi": int(float(row['OI'].replace(',', '')))
                    })
                except Exception as e:
                    logger.warning(f"Error parsing row {row}: {e}")
                    continue
        
        # Sort newest first (though CSV should already be sorted)
        candles.sort(key=lambda x: x['date'], reverse=True)
        return candles[:n_days]
        
    except Exception as e:
        logger.error(f"Error reading MCX CSV for {commodity}: {e}")
        return []
