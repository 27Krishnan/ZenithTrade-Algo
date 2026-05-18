from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from loguru import logger


@dataclass
class CommodityStrategyRuntime:
    slug: str
    name: str
    package: str
    instruments: list[str]
    color: str
    history_default_instrument: str
    started: bool = False
    start_error: str | None = None
    scheduler: Any = None
    _db_mod: Any = field(default=None, init=False, repr=False)
    _monitor_mod: Any = field(default=None, init=False, repr=False)
    _scheduler_mod: Any = field(default=None, init=False, repr=False)
    _backtester_mod: Any = field(default=None, init=False, repr=False)

    def _load_modules(self):
        if self._db_mod:
            return
        self._db_mod = import_module(f"{self.package}.database")
        self._monitor_mod = import_module(f"{self.package}.monitor")
        self._scheduler_mod = import_module(f"{self.package}.scheduler")
        self._backtester_mod = import_module(f"{self.package}.backtester")

    def start(self):
        if self.started:
            return

        try:
            self._load_modules()
            self._db_mod.init_db()
            self._monitor_mod.load_today_states()
            self._monitor_mod.start_monitor()
            self.scheduler = self._scheduler_mod.start_scheduler()
            self.started = True
            self.start_error = None
            logger.info(f"Strategy runtime ready: {self.name}")
        except Exception as exc:
            self.start_error = str(exc)
            logger.exception(f"Strategy runtime failed to start: {self.name}")

    def shutdown(self):
        if self.scheduler:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                logger.debug(f"Scheduler shutdown skipped for {self.name}")
        self.scheduler = None
        self.started = False

    def fetch_now(self):
        self._load_modules()
        return self._scheduler_mod.fetch_now()

    def get_live(self) -> dict[str, dict]:
        self._load_modules()
        return self._monitor_mod.get_all_live()

    def run_backtest(self, instrument: str, date_str: str) -> dict:
        self._load_modules()
        return self._backtester_mod.run_backtest(instrument.upper(), date_str)

    def sync_live(self, instrument: str, type: str, sim: dict) -> dict:
        self._load_modules()
        return self._monitor_mod.sync_live(instrument.upper(), type, sim)

    def get_settings(self) -> dict:
        self._load_modules()
        db = self._db_mod.Session()
        try:
            settings = db.query(self._db_mod.Settings).all()
            return {s.key: s.value for s in settings}
        finally:
            db.close()

    def update_settings(self, payload: dict):
        self._load_modules()
        for k, v in payload.items():
            # Convert bool to string if needed
            if isinstance(v, bool):
                v = "true" if v else "false"
            self._db_mod.set_setting(k, v)
        return {"success": True}

    def save_instrument_defaults(self, instrument: str, auto_trade: bool, levels: dict):
        self._load_modules()
        import json
        payload = {
            f"auto_trade_{instrument}": "true" if auto_trade else "false",
            f"default_levels_{instrument}": json.dumps(levels)
        }
        return self.update_settings(payload)

    def history_rows(self, limit: int = 30) -> list[dict]:
        self._load_modules()
        db = self._db_mod.Session()
        try:
            rows = (
                db.query(self._db_mod.DailyState)
                .order_by(self._db_mod.DailyState.date.desc())
                .limit(limit)
                .all()
            )
            result = []
            for row in rows:
                result.append(
                    {
                        "strategy": self.slug,
                        "strategy_name": self.name,
                        "id": row.id,
                        "date": row.date,
                        "instrument": row.instrument,
                        "trading_symbol": row.trading_symbol,
                        "long_state": row.long_state,
                        "short_state": row.short_state,
                        "long_pnl": row.long_pnl or 0,
                        "short_pnl": row.short_pnl or 0,
                        "total_pnl": (row.long_pnl or 0) + (row.short_pnl or 0),
                        "exec_time": row.fetched_at,
                    }
                )
            return result
        finally:
            db.close()

    def history_detail(self, history_id: str) -> dict:
        self._load_modules()
        db = self._db_mod.Session()
        try:
            row = (
                db.query(self._db_mod.DailyState)
                .filter(
                    (self._db_mod.DailyState.id == history_id)
                    | (self._db_mod.DailyState.date == history_id)
                )
                .first()
            )
            instrument = row.instrument if row else self.history_default_instrument
            backtest_date = row.date if row else history_id
        finally:
            db.close()

        return self.run_backtest(instrument, backtest_date)

    def overview(self) -> dict:
        lives = self.get_live()
        cards = []
        active_positions = 0
        pending_positions = 0
        gap_positions = 0

        for instrument in self.instruments:
            state = lives.get(instrument, {})
            long_state = state.get("long_state", "UNKNOWN")
            short_state = state.get("short_state", "UNKNOWN")
            active_positions += sum(
                1 for side in (long_state, short_state) if side in ("ACTIVE_P1", "ACTIVE_P2")
            )
            pending_positions += sum(
                1 for side in (long_state, short_state) if side == "PENDING"
            )
            gap_positions += sum(1 for side in (long_state, short_state) if side == "GAP")
            cards.append(
                {
                    "instrument": instrument,
                    "trading_symbol": state.get("trading_symbol"),
                    "ltp": state.get("ltp"),
                    "last_update": state.get("last_update"),
                    "long_state": long_state,
                    "short_state": short_state,
                    "long_entry_date": state.get("long_entry_date"),
                    "short_entry_date": state.get("short_entry_date"),
                    "long_lot1_closed": state.get("long_lot1_closed", False),
                    "short_lot1_closed": state.get("short_lot1_closed", False),
                    "long_pnl": state.get("long_pnl", 0),
                    "short_pnl": state.get("short_pnl", 0),
                    "auto_trade": state.get("auto_trade", False),
                    "long_gap_recovered": state.get("long_gap_recovered", False),
                    "short_gap_recovered": state.get("short_gap_recovered", False),
                }
            )

        return {
            "slug": self.slug,
            "name": self.name,
            "color": self.color,
            "started": self.started,
            "error": self.start_error,
            "instrument_count": len(self.instruments),
            "active_positions": active_positions,
            "pending_positions": pending_positions,
            "gap_positions": gap_positions,
            "instruments": cards,
            "live": lives,
        }


class StrategyRegistry:
    def __init__(self):
        self._strategies = {
            "gold": CommodityStrategyRuntime(
                slug="gold",
                name="GOLD • MathZing",
                package="gold_strategy",
                instruments=["GOLD", "GOLDM"],
                color="#d4af37",
                history_default_instrument="GOLD",
            ),
            "silver": CommodityStrategyRuntime(
                slug="silver",
                name="SILVER • MathZing",
                package="silver_strategy",
                instruments=["SILVER", "SILVERM", "SILVERMIC"],
                color="#9fb0c0",
                history_default_instrument="SILVER",
            ),
            "natural-gas": CommodityStrategyRuntime(
                slug="natural-gas",
                name="NG • MathZing",
                package="natural_gas_strategy",
                instruments=["NATURALGAS", "NATURALGASM"],
                color="#27c3a7",
                history_default_instrument="NATURALGAS",
            ),
            "nifty": CommodityStrategyRuntime(
                slug="nifty",
                name="NIFTY • MathZing",
                package="nifty_strategy",
                instruments=["NIFTY"],
                color="#00d4aa",
                history_default_instrument="NIFTY",
            ),
        }

    def start_all(self):
        for strategy in self._strategies.values():
            strategy.start()

    def shutdown_all(self):
        for strategy in self._strategies.values():
            strategy.shutdown()

    def list(self) -> list[CommodityStrategyRuntime]:
        return list(self._strategies.values())

    def get(self, slug: str) -> CommodityStrategyRuntime:
        strategy = self._strategies.get(slug)
        if not strategy:
            raise KeyError(f"Unknown strategy: {slug}")
        return strategy

    def overview(self) -> dict:
        strategies = [strategy.overview() for strategy in self.list()]
        totals = {
            "strategies": len(strategies),
            "instruments": sum(item["instrument_count"] for item in strategies),
            "active_positions": sum(item["active_positions"] for item in strategies),
            "pending_positions": sum(item["pending_positions"] for item in strategies),
            "gap_positions": sum(item["gap_positions"] for item in strategies),
        }
        return {"totals": totals, "strategies": strategies}

    def history(self, slug: str | None = None, limit: int = 30) -> list[dict]:
        if slug:
            return self.get(slug).history_rows(limit=limit)

        rows: list[dict] = []
        per_strategy_limit = max(1, limit)
        for strategy in self.list():
            rows.extend(strategy.history_rows(limit=per_strategy_limit))
        rows.sort(key=lambda item: (item["date"], item["strategy"], item["instrument"]), reverse=True)
        return rows[:limit]


strategy_registry = StrategyRegistry()
