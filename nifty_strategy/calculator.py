"""
Nifty Strategy Calculator
All prices rounded to nearest 0.05 tick.
"""
from dataclasses import dataclass
from typing import Optional
import json
from loguru import logger

def rt(value: float, tick: float = 0.05) -> float:
    """Round to nearest tick."""
    if value is None: return 0.0
    return round(round(value / tick) * tick, 2)


@dataclass
class DayCandle:
    date: str
    high: float
    low: float


@dataclass
class NiftyLevels:
    instrument: str
    trading_symbol: str
    token: str
    raw_days: list  # list of DayCandle (newest first)

    # Core OHLC windows
    h2: float = 0.0
    l2: float = 0.0

    # Entry & Target (Calculated using 0.125% and 1.25% rules)
    e_l: float = 0.0   # Long entry: 2DHH * (1 + 0.125%)
    e_s: float = 0.0   # Short entry: 2DLL * (1 - 0.125%)
    t_l: float = 0.0   # Long target: Entry * (1 + 1.25%)
    t_s: float = 0.0   # Short target: Entry * (1 - 1.25%)

    # SL Phase 1 — Long
    # SL1: Max(Entry * (1 - 1.25%), 2DLL * (1 - 0.125%))
    sl1_long_a: float = 0.0
    sl1_long_b: float = 0.0
    sl1_long:   float = 0.0

    # SL Phase 1 — Short
    # SL1: Min(Entry * (1 + 1.25%), 2DHH * (1 + 0.125%))
    sl1_short_a: float = 0.0
    sl1_short_b: float = 0.0
    sl1_short:   float = 0.0

    # SL Phase 2 — Long (Trailing)
    # SL2: Max(Entry, 2DLL * (1 - 0.125%))
    sl2_long_a: float = 0.0
    sl2_long_b: float = 0.0
    sl2_long:   float = 0.0

    # SL Phase 2 — Short
    # SL2: Min(Entry, 2DHH * (1 + 0.125%))
    sl2_short_a: float = 0.0
    sl2_short_b: float = 0.0
    sl2_short:   float = 0.0

    def __post_init__(self):
        days = self.raw_days  # newest → oldest
        if len(days) < 2:
            return
        # Use 2-day lookback
        self.h2 = max(d["high"]  for d in days[:2])
        self.l2 = min(d["low"]   for d in days[:2])
        self._calc()

    @property
    def h4(self) -> float:
        """Compatibility alias for shared monitor/database code."""
        return self.h2

    @property
    def l4(self) -> float:
        """Compatibility alias for shared monitor/database code."""
        return self.l2

    def _calc(self):
        # 1. Entry Calculations (0.125% Buffer)
        self.e_l = rt(self.h2 * 1.00125)
        self.e_s = rt(self.l2 * 0.99875)

        # 2. Target Calculations (1.25% Move)
        self.t_l = rt(self.e_l * 1.0125)
        self.t_s = rt(self.e_s * 0.9875)

        # 3. Phase 1 SL Calculations
        # Long SL1: Max ( Entry * (1 - 1.25%) OR 2DLL * (1 - 0.125%) )
        self.sl1_long_a = rt(self.e_l * 0.9875)
        self.sl1_long_b = rt(self.l2 * 0.99875)
        self.sl1_long   = max(self.sl1_long_a, self.sl1_long_b)

        # Short SL1: Min ( Entry * (1 + 1.25%) OR 2DHH * (1 + 0.125%) )
        self.sl1_short_a = rt(self.e_s * 1.0125)
        self.sl1_short_b = rt(self.h2 * 1.00125)
        self.sl1_short   = min(self.sl1_short_a, self.sl1_short_b)

        # 4. Phase 2 SL Calculations (Trailing)
        # Long SL2: Max ( Entry OR 2DLL * (1 - 0.125%) )
        self.sl2_long_a = self.e_l
        self.sl2_long_b = rt(self.l2 * 0.99875)
        self.sl2_long   = max(self.sl2_long_a, self.sl2_long_b)

        # Short SL2: Min ( Entry OR 2DHH * (1 + 0.125%) )
        self.sl2_short_a = self.e_s
        self.sl2_short_b = rt(self.h2 * 1.00125)
        self.sl2_short   = min(self.sl2_short_a, self.sl2_short_b)

    def update_from_actual_entry(self, actual_entry: float, side: str):
        """
        Force-update Target and SL Part A based on the price we ACTUALLY traded at (e.g. after a gap).
        side: 'long' or 'short'
        """
        if side == "long":
            self.e_l = actual_entry
            self.t_l = rt(actual_entry * 1.0125)
            self.sl1_long_a = rt(actual_entry * 0.9875)
            self.sl1_long = max(self.sl1_long_a, self.sl1_long_b)
            self.sl2_long_a = actual_entry
            self.sl2_long = max(self.sl2_long_a, self.sl2_long_b)
        else:
            self.e_s = actual_entry
            self.t_s = rt(actual_entry * 0.9875)
            self.sl1_short_a = rt(actual_entry * 1.0125)
            self.sl1_short = min(self.sl1_short_a, self.sl1_short_b)
            self.sl2_short_a = actual_entry
            self.sl2_short = min(self.sl2_short_a, self.sl2_short_b)

    def to_dict(self) -> dict:
        return {
            "instrument":     self.instrument,
            "trading_symbol": self.trading_symbol,
            "token":          self.token,
            "raw_days":       self.raw_days,
            "lookback_count": 2,
            "h2": self.h2, "l2": self.l2,
            "h4": self.h4, "l4": self.l4, # Aliases
            "e_l": self.e_l, "e_s": self.e_s,
            "t_l": self.t_l, "t_s": self.t_s,
            "sl1_long":  {"a": self.sl1_long_a,  "b": self.sl1_long_b,  "sl": self.sl1_long},
            "sl1_short": {"a": self.sl1_short_a, "b": self.sl1_short_b, "sl": self.sl1_short},
            "sl2_long":  {"a": self.sl2_long_a,  "b": self.sl2_long_b,  "sl": self.sl2_long},
            "sl2_short": {"a": self.sl2_short_a, "b": self.sl2_short_b, "sl": self.sl2_short},
        }

def fetch_and_calculate(instrument: str, trading_symbol: str, token: str):
    """
    Fetches 2 days of historical data from NSE India and calculates levels.
    """
    from .nse_fetcher import nse_fetcher
    try:
        # Determine current month's expiry or use a fixed one if provided
        # For now, we follow the user's example for May 2026
        expiry = "26-May-2026" 
        
        logger.info(f"Nifty Calculator: Fetching historical data from NSE for {expiry}")
        nse_data = nse_fetcher.fetch_nifty_futures(expiry_date=expiry)
        
        if not nse_data or len(nse_data) < 2:
            logger.error(f"Nifty Calculator: Not enough data from NSE for {expiry}")
            return None

        # NSE data is already sorted newest first
        # Take the last 2 COMPLETED days
        raw_days = []
        selected_dates = []
        for row in nse_data[:2]:
            raw_days.append({
                "date": row['date'],
                "high": float(row['high']),
                "low":  float(row['low'])
            })
            selected_dates.append(row['date'])
        
        logger.info(f"Nifty Calculator (NSE Source): Selected Lookback Dates: {', '.join(selected_dates)}")
            
        lvls = NiftyLevels(instrument, trading_symbol, token, raw_days)
        return lvls.to_dict()
        
    except Exception as e:
        logger.error(f"Nifty Calculator NSE Error: {e}")
        return None
