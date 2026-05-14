"""
Persistent strategy state — stored in SQLite.
One row per instrument per day.
"""
import json
from datetime import datetime, date
from sqlalchemy import create_engine, Column, String, Float, Boolean, Text, Date
from sqlalchemy.orm import declarative_base, sessionmaker
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "natural_gas_strategy.db")
engine  = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
Base    = declarative_base()


class DailyState(Base):
    __tablename__ = "daily_state"

    id               = Column(String, primary_key=True)   # "{date}_{instrument}"
    date             = Column(String, index=True)
    instrument       = Column(String)                     # "GOLD" | "GOLDM"
    trading_symbol   = Column(String)
    token            = Column(String)
    lot_size         = Column(Float, default=1)

    # OHLC windows
    h4 = Column(Float, default=0)
    l4 = Column(Float, default=0)
    h2 = Column(Float, default=0)
    l2 = Column(Float, default=0)

    # Calculated levels (JSON stored as Text)
    levels_json      = Column(Text, default="{}")

    # Trade state
    long_state       = Column(String, default="PENDING")   # PENDING|GAP|ACTIVE_P1|ACTIVE_P2|CLOSED
    short_state      = Column(String, default="PENDING")
    long_entry_price = Column(Float,  nullable=True)
    long_entry_date  = Column(String, nullable=True)
    short_entry_price= Column(Float,  nullable=True)
    short_entry_date = Column(String, nullable=True)
    long_lot1_closed = Column(Boolean, default=False)
    short_lot1_closed= Column(Boolean, default=False)
    long_exit_price  = Column(Float,  nullable=True)
    short_exit_price = Column(Float,  nullable=True)
    long_exit_reason = Column(String, nullable=True)
    short_exit_reason= Column(String, nullable=True)
    long_pnl         = Column(Float,  default=0)
    short_pnl        = Column(Float,  default=0)

    # Auto-trade setting
    auto_trade       = Column(Boolean, default=False)

    # Session tracking
    morning_processed = Column(Boolean, default=False)

    # Timestamps
    fetched_at       = Column(String, nullable=True)
    created_at       = Column(String, default=lambda: datetime.now().isoformat())

    @property
    def levels(self) -> dict:
        try:
            return json.loads(self.levels_json or "{}")
        except Exception:
            return {}

    @levels.setter
    def levels(self, val: dict):
        self.levels_json = json.dumps(val)


class Settings(Base):
    __tablename__ = "settings"
    key   = Column(String, primary_key=True)
    value = Column(String)


def init_db():
    Base.metadata.create_all(engine)
    # Seed default settings
    db = Session()
    try:
        for key, default in [
            ("telegram_chat_id", ""),
            ("auto_trade_naturalgas", "false"),
            ("auto_trade_naturalgasm", "false"),
            ("tg_notify_morning", "true"),
            ("tg_notify_market_open", "true"),
            ("tg_notify_entry", "true"),
            ("tg_notify_exit", "true"),
            ("tg_notify_gap", "true"),
        ]:
            if not db.query(Settings).filter_by(key=key).first():
                db.add(Settings(key=key, value=default))

        db.commit()
    finally:
        db.close()


def get_today_state(instrument: str, db=None) -> DailyState | None:
    close_db = db is None
    if close_db:
        db = Session()
    try:
        today = date.today().isoformat()
        row_id = f"{today}_{instrument}"
        return db.query(DailyState).filter_by(id=row_id).first()
    finally:
        if close_db:
            db.close()


def get_active_state(instrument: str):
    """Find the most recent row with an active position (searches last 7 rows)."""
    db = Session()
    try:
        recent_rows = db.query(DailyState).filter(
            DailyState.instrument == instrument
        ).order_by(DailyState.date.desc()).limit(7).all()

        for row in recent_rows:
            if (row.long_state in ("ACTIVE_P1", "ACTIVE_P2") or
                    row.short_state in ("ACTIVE_P1", "ACTIVE_P2")):
                return row
        return None
    finally:
        db.close()


def upsert_state(instrument: str, data: dict, db=None) -> DailyState:
    close_db = db is None
    if close_db:
        db = Session()
    try:
        today  = date.today().isoformat()
        row_id = f"{today}_{instrument}"
        row    = db.query(DailyState).filter_by(id=row_id).first()
        if not row:
            row = DailyState(id=row_id, date=today, instrument=instrument)
            db.add(row)
        for k, v in data.items():
            setattr(row, k, v)
        db.commit()
        db.refresh(row)
        return row
    finally:
        if close_db:
            db.close()


def get_setting(key: str, default: str = "") -> str:
    db = Session()
    try:
        row = db.query(Settings).filter_by(key=key).first()
        return row.value if row else default
    finally:
        db.close()


def set_setting(key: str, value: str):
    db = Session()
    try:
        row = db.query(Settings).filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.add(Settings(key=key, value=value))
        db.commit()
    finally:
        db.close()

