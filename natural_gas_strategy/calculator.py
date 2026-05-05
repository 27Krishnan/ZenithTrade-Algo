"""
Natural Gas Strategy Calculator
All prices rounded to nearest 0.05 tick.
"""
from dataclasses import dataclass, field
from typing import Optional


def rt(value: float, tick: float = 0.05) -> float:
    """Round to nearest tick."""
    return round(round(value / tick) * tick, 2)


@dataclass
class DayCandle:
    date: str
    high: float
    low: float


@dataclass
class NaturalGasLevels:
    instrument: str
    trading_symbol: str
    token: str
    raw_days: list  # list of DayCandle (newest first)

    # Core OHLC windows (calculated after __post_init__)
    h3: float = 0.0
    l3: float = 0.0
    h2: float = 0.0
    l2: float = 0.0

    # Entry & Target
    e_l: float = 0.0   # Long entry
    e_s: float = 0.0   # Short entry
    t_l: float = 0.0   # Lot-1 Long target
    t_s: float = 0.0   # Lot-1 Short target

    # SL Phase 1 — Long
    sl1_long_a: float = 0.0
    sl1_long_b: float = 0.0
    sl1_long:   float = 0.0

    # SL Phase 1 — Short
    sl1_short_a: float = 0.0
    sl1_short_b: float = 0.0
    sl1_short:   float = 0.0

    # SL Phase 2 — Long (Lot-2 only, after Lot-1 target hit)
    sl2_long_a: float = 0.0
    sl2_long_b: float = 0.0
    sl2_long:   float = 0.0

    # SL Phase 2 — Short
    sl2_short_a: float = 0.0
    sl2_short_b: float = 0.0
    sl2_short:   float = 0.0

    def __post_init__(self):
        days = self.raw_days  # newest → oldest
        if len(days) < 3:
            return
        self.h3 = max(d["high"]  for d in days[:3])
        self.l3 = min(d["low"]   for d in days[:3])
        self.h2 = max(d["high"]  for d in days[:2])
        self.l2 = min(d["low"]   for d in days[:2])
        self._calc()

    @property
    def h4(self) -> float:
        """Compatibility alias for shared monitor/database code."""
        return self.h3

    @property
    def l4(self) -> float:
        """Compatibility alias for shared monitor/database code."""
        return self.l3

    def _calc(self):
        # Entries
        self.e_l = rt(self.h3 * 1.004)
        self.e_s = rt(self.l3 * 0.996)

        # Lot-1 Targets
        self.t_l = rt(self.e_l * 1.04)
        self.t_s = rt(self.e_s * 0.96)

        # Phase 1 SL — Long
        self.sl1_long_a = rt(self.e_l * 0.96)
        self.sl1_long_b = rt(self.l2 * 0.996)
        self.sl1_long   = max(self.sl1_long_a, self.sl1_long_b)

        # Phase 1 SL — Short
        self.sl1_short_a = rt(self.e_s * 1.04)
        self.sl1_short_b = rt(self.h2 * 1.004)
        self.sl1_short   = min(self.sl1_short_a, self.sl1_short_b)

        # Phase 2 SL — Long
        self.sl2_long_a = rt(self.e_l * 0.96)
        self.sl2_long_b = rt(self.l3 * 0.996)
        self.sl2_long   = max(self.sl2_long_a, self.sl2_long_b)

        # Phase 2 SL — Short
        self.sl2_short_a = rt(self.e_s * 1.04)
        self.sl2_short_b = rt(self.h3 * 1.004)
        self.sl2_short   = min(self.sl2_short_a, self.sl2_short_b)

    def update_from_actual_entry(self, actual_entry: float, side: str):
        """
        Force-update Target and SL Part A based on the price we ACTUALLY traded at (e.g. after a gap).
        side: 'long' or 'short'
        """
        if side == "long":
            self.e_l = actual_entry
            self.t_l = rt(actual_entry * 1.04)
            self.sl1_long_a = rt(actual_entry * 0.96)
            self.sl1_long = max(self.sl1_long_a, self.sl1_long_b)
            self.sl2_long_a = rt(actual_entry * 0.96)
            self.sl2_long = max(self.sl2_long_a, self.sl2_long_b)
        else:
            self.e_s = actual_entry
            self.t_s = rt(actual_entry * 0.96)
            self.sl1_short_a = rt(actual_entry * 1.04)
            self.sl1_short = min(self.sl1_short_a, self.sl1_short_b)
            self.sl2_short_a = rt(actual_entry * 1.04)
            self.sl2_short = min(self.sl2_short_a, self.sl2_short_b)

    def to_dict(self) -> dict:
        return {
            "instrument":     self.instrument,
            "trading_symbol": self.trading_symbol,
            "token":          self.token,
            "raw_days":       self.raw_days,
            "lookback_count": 3,
            "h3": self.h3, "l3": self.l3,
            "h4": self.h4, "l4": self.l4,
            "h2": self.h2, "l2": self.l2,
            "e_l": self.e_l, "e_s": self.e_s,
            "t_l": self.t_l, "t_s": self.t_s,
            "sl1_long":  {"a": self.sl1_long_a,  "b": self.sl1_long_b,  "sl": self.sl1_long},
            "sl1_short": {"a": self.sl1_short_a, "b": self.sl1_short_b, "sl": self.sl1_short},
            "sl2_long":  {"a": self.sl2_long_a,  "b": self.sl2_long_b,  "sl": self.sl2_long},
            "sl2_short": {"a": self.sl2_short_a, "b": self.sl2_short_b, "sl": self.sl2_short},
        }
