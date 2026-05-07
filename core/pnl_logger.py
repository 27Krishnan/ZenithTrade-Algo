"""
P&L Logger — writes closed trades to papertrading.db trades table
so the P&L Report tab shows realized profits and losses.
"""
import sqlite3
import os
from datetime import datetime
from loguru import logger

# Path to main papertrading DB (relative to project root)
_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "papertrading.db")


from core.utils import get_now_ist

def log_closed_trade(
    instrument: str,
    trading_symbol: str,
    direction: str,          # "LONG" or "SHORT"
    entry_price: float,
    exit_price: float,
    entry_date: str,         # ISO string or human readable e.g. "21 Apr 09:10"
    exit_reason: str,        # "SL_HIT", "SL2_HIT", "TARGET_HIT", "MANUAL"
    lots: int,               # number of lots closed (1 or 2)
    lot_size: float,         # contract multiplier e.g. 100 for Gold, 30 for Silver
    strategy: str = "",      # e.g. "GOLD • MathZing"
    realized_lot1_pnl: float = 0.0,  # already booked P&L from Lot-1 (if any)
):
    """
    Insert a closed trade row into papertrading.db trades table.
    Standardized to IST.
    """
    try:
        action = "BUY" if direction == "LONG" else "SELL"
        if direction == "LONG":
            gross_pnl = round((exit_price - entry_price) * lots * lot_size + realized_lot1_pnl, 2)
        else:
            gross_pnl = round((entry_price - exit_price) * lots * lot_size + realized_lot1_pnl, 2)

        now_ist = get_now_ist()
        now_str = now_ist.isoformat()

        db_path = os.path.abspath(_DB_PATH)
        if not os.path.exists(db_path):
            logger.warning(f"pnl_logger: papertrading.db not found at {db_path}")
            return

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Robust entry date parsing
        entry_dt_str = None
        if entry_date:
            try:
                # Try ISO format first
                entry_dt = datetime.fromisoformat(entry_date)
                entry_dt_str = entry_dt.strftime("%Y-%m-%d %H:%M:%S.000000")
            except ValueError:
                # Fallback for old format "dd MMM HH:MM"
                try:
                    year = now_ist.year
                    entry_dt = datetime.strptime(f"{entry_date} {year}", "%d %b %H:%M %Y")
                    entry_dt_str = entry_dt.strftime("%Y-%m-%d %H:%M:%S.000000")
                except Exception:
                    entry_dt_str = now_str
        else:
            entry_dt_str = now_str

        cur.execute("""
            INSERT INTO trades (
                symbol, exchange, instrument_type, action, trade_type,
                entry_price, exit_price, exit_reason,
                quantity, lot_size,
                stop_loss,
                gross_pnl, net_pnl,
                status,
                entry_triggered_at, closed_at,
                created_at, updated_at,
                strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trading_symbol,
            "MCX" if instrument not in ("NIFTY", "BANKNIFTY") else "NFO",
            "FUT",
            action,
            "POSITIONAL",
            entry_price,
            exit_price,
            exit_reason,
            lots,
            lot_size,
            0.0,  # stop_loss
            gross_pnl,
            gross_pnl,
            "CLOSED",
            entry_dt_str,
            now_str,
            now_str,
            now_str,
            strategy,
        ))
        conn.commit()
        conn.close()
        logger.info(
            f"pnl_logger: Logged {instrument} {direction} | "
            f"Entry={entry_price} Exit={exit_price} Lots={lots}x{lot_size} "
            f"PnL={gross_pnl} Reason={exit_reason}"
        )
    except Exception as e:
        logger.error(f"pnl_logger: Failed to log trade for {instrument}: {e}")
