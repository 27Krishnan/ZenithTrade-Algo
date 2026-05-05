"""
Nifty Strategy Scheduler
========================
Schedules morning data fetch (9:00 AM) using APScheduler.
"""
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import threading
from loguru import logger
from datetime import datetime
from .database import init_db

IST = pytz.timezone("Asia/Kolkata")

def fetch_now(broadcast=None):
    """Manual trigger to fetch levels for today's Nifty futures."""
    logger.info(f"Nifty Scheduler: Fetching levels (requested_broadcast={broadcast})...")
    from data.angel_api import angel_api
    from .calculator import fetch_and_calculate
    from .monitor import set_levels_from_nifty_levels

    # 1. Find symbols for current month Nifty futures
    nifty_sym = angel_api.get_current_future_symbol("NIFTY", exchange="NFO")

    configs = []
    if nifty_sym:
        configs.append(("NIFTY", nifty_sym["symbol"], nifty_sym["token"]))

    for inst, sym, tok in configs:
        logger.info(f"Nifty Scheduler: Processing {inst} ({sym})")
        levels = fetch_and_calculate(inst, sym, tok)
        if levels:
            from .calculator import NiftyLevels
            lvls_obj = NiftyLevels(
                instrument=inst,
                trading_symbol=sym,
                token=tok,
                raw_days=levels["raw_days"]
            )
            set_levels_from_nifty_levels(inst, lvls_obj)
            logger.success(f"Nifty Levels updated for {inst}")
    # 3. Send Telegram Alert
    should_broadcast = False
    if broadcast is True:
        should_broadcast = True
    elif broadcast is False:
        should_broadcast = False
    else:
        # Auto-detect mode
        from .database import get_setting, set_setting
        now = datetime.now(IST)
        today = now.date().isoformat()
        
        # BROADCAST RULES:
        # Morning: 08:30 AM
        if now.hour == 8 and now.minute >= 25:
             should_broadcast = True
             logger.info(f"Nifty Strategy: Broadcasting morning briefing (Time: {now.strftime('%H:%M')})")

    if configs and should_broadcast:
        from .monitor import get_all_live
        from .telegram import send_morning_alert
        from .database import get_today_state
        
        levels_map = {}
        for inst, _, _ in configs:
            state = get_today_state(inst)
            if state:
                levels_map[inst] = state.levels
        
        if levels_map:
            send_morning_alert(levels_map, get_all_live())
            logger.info("Nifty Scheduler: Morning alert sent")
            # Ensure recorded
            from .database import set_setting
            now = datetime.now(IST)
            set_setting("last_morning_briefing_date", now.date().isoformat())

def start_scheduler():
    init_db()
    
    sched = BackgroundScheduler(timezone=IST)
    
    # 1. Fetch Data - 08:05 AM (Mon-Fri)
    sched.add_job(fetch_now, "cron", day_of_week="mon-fri", hour=8, minute=5, args=[False], id="nifty_morning_fetch")
    
    # 2. Send Alert - 08:30 AM (Mon-Fri)
    sched.add_job(fetch_now, "cron", day_of_week="mon-fri", hour=8, minute=30, args=[True], id="nifty_morning_alert")
    
    sched.start()
    logger.info("Nifty Scheduler started: Fetch @ 08:05 AM, Alert @ 08:30 AM")
    return sched
