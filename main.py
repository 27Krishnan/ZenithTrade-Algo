"""
Paper Trading System - Entry Point
Run: uvicorn main:app --host 0.0.0.0 --port 8000
"""

# Dashboard Main Entry Point
import os
import sys
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger

# Configure logger before anything else
# Force UTF-8 on Windows console to handle emoji in log messages
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logger.remove()
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level} | {message}", level="INFO")
os.makedirs("logs", exist_ok=True)
logger.add("logs/papertrading.log", rotation="1 day", retention="7 days", level="DEBUG")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────
    from database.db import init_db
    from data.angel_api import angel_api
    from data.market_feed import market_feed
    from scheduler.market_sessions import market_scheduler
    from core.strategy_registry import strategy_registry
    from config.settings import settings
    from loguru import logger

    init_db()
    logger.info("Database ready")

    # Check Angel One credentials and connect
    if settings.ANGEL_CLIENT_ID and settings.ANGEL_API_KEY:
        connected = angel_api.connect()
        if not connected:
            logger.warning("Angel One connection failed - running in offline mode")
        else:
            # Pre-cache instrument master for option chain
            try:
                from api.option_chain import load_master

                logger.info("Loading instrument master...")
                master = load_master()
                logger.info(f"Instrument master cached: {len(master)} instruments")
            except Exception as e:
                logger.warning(f"Could not cache instrument master: {e}")
    else:
        logger.warning("Angel One credentials not configured - running in demo mode")

    def _angel_retry_loop():
        if not (settings.ANGEL_CLIENT_ID and settings.ANGEL_API_KEY):
            return
        import time
        for attempt in range(1, 11):
            if angel_api.is_connected():
                return
            wait_seconds = min(30, attempt * 3)
            logger.info(f"Angel One retry scheduled in {wait_seconds}s (attempt {attempt}/10)")
            time.sleep(wait_seconds)
            if angel_api.reconnect():
                logger.info("Angel One recovered by startup retry")
                try:
                    from data.market_feed import market_feed
                    if market_feed._subscriptions:
                        market_feed.start()
                except Exception as exc:
                    logger.debug(f"Market feed restart skipped after Angel retry: {exc}")
                return
        logger.error("Angel One startup retry exhausted - use dashboard Angel pill to retry")

    if settings.ANGEL_CLIENT_ID and settings.ANGEL_API_KEY and not angel_api.is_connected():
        import threading
        threading.Thread(target=_angel_retry_loop, daemon=True, name="AngelStartupRetry").start()

    # Check EasyOCR availability
    try:
        from parsers.signal_parser import _get_reader
        logger.info("Checking EasyOCR availability...")
        _get_reader()
        logger.info("EasyOCR ready")
    except Exception as e:
        logger.warning(f"EasyOCR not available: {e}")

    # Load engine active trades
    from core.engine import engine
    engine._load_active_trades()

    # Start LTP poller
    from core.ltp_poller import ltp_poller
    ltp_poller.start()

    # START STRATEGIES IN BACKGROUND
    import threading
    import time
    def _bg_start():
        try:
            logger.info("Starting strategies in background with 5s delay...")
            # We delay the very first start to ensure the app is fully ready
            time.sleep(5)
            for slug, strategy in strategy_registry._strategies.items():
                logger.info(f"Starting {slug}...")
                strategy.start()
                time.sleep(5) # Generous breath for API rate limits
            
            market_scheduler.start()
            logger.info("Paper Trading System READY (Background tasks completed)")
        except Exception as e:
            logger.error(f"Background startup failed: {e}")

    threading.Thread(target=_bg_start, daemon=True).start()

    logger.info("FastAPI Server READY - Dashboard available at http://localhost:8000")
    yield  # ── App running ──────────────────────────────────────

    # ── Shutdown ───────────────────────────────────────────────
    from core.ltp_poller import ltp_poller
    from core.strategy_registry import strategy_registry

    ltp_poller.stop()
    strategy_registry.shutdown_all()
    market_feed.stop()
    angel_api.disconnect()
    
    # Only stop if it was started
    if market_scheduler.scheduler and market_scheduler.scheduler.running:
        market_scheduler.stop()
    
    logger.info("Paper Trading System stopped")


# Patch the app to use lifespan
from api.main import app

app.router.lifespan_context = lifespan


if __name__ == "__main__":
    from config.settings import settings

    uvicorn.run(
        "main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.DEBUG,
    )
