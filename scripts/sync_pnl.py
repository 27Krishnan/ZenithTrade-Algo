import os
import sqlite3
import json
import sys
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.pnl_logger import log_closed_trade
from loguru import logger

STRATEGIES = {
    "GOLD": {
        "db": "gold_strategy/gold_strategy.db",
        "name": "GOLD • MathZing",
        "multiplier": 100
    },
    "SILVER": {
        "db": "silver_strategy/silver_strategy.db",
        "name": "SILVER • MathZing",
        "multiplier": 30
    },
    "NATURALGAS": {
        "db": "natural_gas_strategy/natural_gas_strategy.db",
        "name": "NATURALGAS • MathZing",
        "multiplier": 1250 # Fallback
    },
    "NIFTY": {
        "db": "nifty_strategy/nifty_strategy.db",
        "name": "NIFTY • MathZing",
        "multiplier": 65 # Fallback
    }
}

MAIN_DB = os.path.join(PROJECT_ROOT, "papertrading.db")

def get_existing_trade_ids():
    """Get unique identifiers for trades already in main DB."""
    if not os.path.exists(MAIN_DB):
        return set()
    
    conn = sqlite3.connect(MAIN_DB)
    cur = conn.cursor()
    # Unique composite key: strategy + symbol + entry_price + action + day
    cur.execute("SELECT strategy, symbol, entry_price, action, closed_at FROM trades")
    rows = cur.fetchall()
    conn.close()
    
    ids = set()
    for r in rows:
        # We use a loose date match (YYYY-MM-DD) for duplicate detection
        date_str = r[4].split('T')[0] if r[4] else ""
        ids.add(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}|{date_str}")
    return ids

def sync_strategy(strat_id, config):
    logger.info(f"Syncing {strat_id} strategy...")
    db_path = os.path.join(PROJECT_ROOT, config["db"])
    if not os.path.exists(db_path):
        logger.warning(f"DB not found: {db_path}")
        return

    existing_ids = get_existing_trade_ids()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    try:
        cur.execute("SELECT * FROM daily_state")
        columns = [description[0] for description in cur.description]
        rows = cur.fetchall()
    except Exception as e:
        logger.error(f"Error reading {strat_id} DB: {e}")
        return
    finally:
        conn.close()

    synced_count = 0
    for row in rows:
        data = dict(zip(columns, row))
        inst = data.get("instrument") or strat_id
        sym  = data.get("trading_symbol") or inst
        mult = config["multiplier"]
        
        # Override multiplier for mini contracts if detected
        if "GOLDM" in sym: mult = 10
        elif "SILVERM" in sym: mult = 1
        elif "NATURALGASM" in sym: mult = 250

        # Sync LONG
        if data.get("long_state") == "CLOSED":
            entry_p = data.get("long_entry_price")
            exit_p  = data.get("long_exit_price")
            date    = data.get("date", "")
            if entry_p is None or exit_p is None:
                logger.warning(f"Skipping LONG for {sym} on {date} - missing prices")
                continue
            
            key = f"{config['name']}|{sym}|{entry_p}|BUY|{date}"
            if key not in existing_ids:
                logger.info(f"Logging missing LONG for {sym} on {date}")
                log_closed_trade(
                    instrument=inst, trading_symbol=sym, direction="LONG",
                    entry_price=entry_p, exit_price=exit_p,
                    entry_date=data.get("long_entry_date", date),
                    exit_reason=data.get("long_exit_reason", "BACKFILL"),
                    lots=2, lot_size=mult, strategy=config["name"]
                )
                synced_count += 1
            else:
                logger.debug(f"Skipping existing LONG for {sym} on {date}")

        # Sync SHORT
        if data.get("short_state") == "CLOSED":
            entry_p = data.get("short_entry_price")
            exit_p  = data.get("short_exit_price")
            date    = data.get("date", "")
            if entry_p is None or exit_p is None:
                logger.warning(f"Skipping SHORT for {sym} on {date} - missing prices")
                continue

            key = f"{config['name']}|{sym}|{entry_p}|SELL|{date}"
            if key not in existing_ids:
                logger.info(f"Logging missing SHORT for {sym} on {date}")
                log_closed_trade(
                    instrument=inst, trading_symbol=sym, direction="SHORT",
                    entry_price=entry_p, exit_price=exit_p,
                    entry_date=data.get("short_entry_date", date),
                    exit_reason=data.get("short_exit_reason", "BACKFILL"),
                    lots=2, lot_size=mult, strategy=config["name"]
                )
                synced_count += 1
            else:
                logger.debug(f"Skipping existing SHORT for {sym} on {date}")

    logger.info(f"Finished {strat_id}: {synced_count} trades synced")

if __name__ == "__main__":
    for sid, conf in STRATEGIES.items():
        sync_strategy(sid, conf)
    logger.success("P&L Synchronization complete!")
