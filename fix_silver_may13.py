import os
import json
from datetime import datetime
from silver_strategy.database import upsert_state, get_today_state, DailyState, Session as SilverSession
from database.db import get_session
from database.models import Trade, TradeStatus, TradeType
from core.utils import get_now_ist

# Data we extracted from backtest for May 13
data = {
    "SILVER": {
        "trading_symbol": "SILVER03JUL26FUT",
        "exchange": "MCX",
        "entry_price": 296159.95,
        "sl2": 290236.75,
        "t_l": 302083.15,
        "lot_size": 30
    },
    "SILVERM": {
        "trading_symbol": "SILVERM30JUN26FUT",
        "exchange": "MCX",
        "entry_price": 297747.85,
        "sl2": 291792.90,
        "t_l": 303702.80,
        "lot_size": 5
    },
    "SILVERMIC": {
        "trading_symbol": "SILVERMIC30JUN26FUT",
        "exchange": "MCX",
        "entry_price": 297749.85,
        "sl2": 291794.85,
        "t_l": 303704.85,
        "lot_size": 1
    }
}

print("Fixing Silver live state for May 13th GAP Recovery...")

# 1. Update internal strategy state in silver_strategy.db
s_db = SilverSession()
try:
    for inst, info in data.items():
        # Update May 13th row
        row_id = f"2026-05-13_{inst}"
        row = s_db.query(DailyState).filter_by(id=row_id).first()
        if not row:
            row = DailyState(id=row_id, date="2026-05-13", instrument=inst)
            s_db.add(row)
        
        row.long_state = "ACTIVE_P2"
        row.long_entry_price = info["entry_price"]
        row.long_entry_date = "2026-05-13"
        row.long_lot1_closed = True
        
        # Optionally, preserve levels if they exist, or just set target/SL2
        try:
            lvls = json.loads(row.levels_json) if row.levels_json else {}
        except:
            lvls = {}
            
        lvls["t_l"] = info["t_l"]
        if "sl2_long" not in lvls:
            lvls["sl2_long"] = {}
        lvls["sl2_long"]["sl"] = info["sl2"]
        row.levels_json = json.dumps(lvls)
        
        # Also copy it to today so monitor picks it up immediately without rebooting
        today = datetime.now().date().isoformat()
        today_row_id = f"{today}_{inst}"
        t_row = s_db.query(DailyState).filter_by(id=today_row_id).first()
        if not t_row:
            t_row = DailyState(id=today_row_id, date=today, instrument=inst)
            s_db.add(t_row)
        
        t_row.long_state = "ACTIVE_P2"
        t_row.long_entry_price = info["entry_price"]
        t_row.long_entry_date = "2026-05-13"
        t_row.long_lot1_closed = True
        t_row.levels_json = json.dumps(lvls)
        
    s_db.commit()
    print("✓ Strategy database (silver_strategy.db) updated.")
finally:
    s_db.close()

# 2. Add trades to papertrading.db so UI shows them
p_db = get_session()
try:
    for inst, info in data.items():
        # Check if already exists to prevent duplicate injection
        existing = p_db.query(Trade).filter(
            Trade.symbol == info["trading_symbol"],
            Trade.status == TradeStatus.OPEN,
            Trade.strategy == "Silver"
        ).first()
        
        if existing:
            # Just update trailing SL
            existing.trailing_sl = info["sl2"]
            print(f"✓ {inst} trade already exists in main UI database, updated SL2.")
        else:
            trade = Trade(
                symbol=info["trading_symbol"],
                exchange=info["exchange"],
                instrument_type="FUT",
                action="BUY",
                trade_type=TradeType.POSITIONAL,
                entry_price=info["entry_price"],
                entry_type="LIMIT",
                quantity=1, # 1 lot remaining
                lot_size=info["lot_size"],
                stop_loss=info["sl2"], # Initial SL doesn't matter much now
                trailing_sl=info["sl2"],
                status=TradeStatus.OPEN,
                target_idx=1, # Target 1 already hit
                strategy="Silver",
                entry_triggered_at=datetime(2026, 5, 13, 9, 15, 0)
            )
            p_db.add(trade)
            print(f"✓ {inst} added to main UI database (papertrading.db).")
            
    p_db.commit()
finally:
    p_db.close()

print("\nFix completed successfully. Please restart the uvicorn server so it loads the updated database values:")
print("pkill -f 'uvicorn.*main:app' && nohup uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 > logs/papertrading.log 2>&1 &")
