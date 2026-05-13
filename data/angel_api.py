import time
import pyotp
import threading
from SmartApi import SmartConnect
from config.settings import settings
from datetime import datetime
from loguru import logger


class AngelOneAPI:
    def __init__(self):
        self.api = None
        self.auth_token = None
        self.feed_token = None
        self._connected = False
        self._monitoring = False
        self._refreshing = False
        self._heartbeat_failures = 0
        self._ltp_cache = {}  # (exchange, token) -> (ltp, timestamp)
        self._cache_lock = threading.Lock()
        self._last_connect_time = None

    def connect(self) -> bool:
        try:
            self.api = SmartConnect(api_key=settings.ANGEL_API_KEY)
            totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
            data = self.api.generateSession(
                settings.ANGEL_CLIENT_ID, settings.ANGEL_PASSWORD, totp
            )
            if data["status"]:
                self.auth_token = data["data"]["jwtToken"]
                self.feed_token = self.api.getfeedToken()
                self._connected = True
                self._heartbeat_failures = 0
                self._last_connect_time = time.time()
                logger.info(f"Angel One connected | Client: {settings.ANGEL_CLIENT_ID}")
                self.start_heartbeat()
                self._start_token_refresh()
                return True
            else:
                logger.error(f"Angel One login failed: {data['message']}")
                return False
        except Exception as e:
            logger.error(f"Angel One connection error: {e}")
            return False

    def start_heartbeat(self):
        """Starts a background thread to check session health every 60s"""
        if self._monitoring:
            return
        self._monitoring = True
        t = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="AngelHeartbeat"
        )
        t.start()
        logger.info("Angel One heartbeat monitor started")

    def _start_token_refresh(self):
        """Proactively refresh JWT token every 55 min to prevent expiry-based disconnects."""
        if self._refreshing:
            return
        self._refreshing = True
        t = threading.Thread(
            target=self._token_refresh_loop, daemon=True, name="AngelTokenRefresh"
        )
        t.start()
        logger.info("Angel One token auto-refresh started (every 55 min)")

    def _token_refresh_loop(self):
        """Silently refreshes Angel One JWT every 55 minutes without UI disconnect flicker."""
        while self._refreshing:
            time.sleep(55 * 60)  # Wait 55 minutes
            if not self._connected:
                continue
            try:
                logger.info("Angel One: Proactive token refresh...")
                new_api = SmartConnect(api_key=settings.ANGEL_API_KEY)
                totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
                data = new_api.generateSession(
                    settings.ANGEL_CLIENT_ID, settings.ANGEL_PASSWORD, totp
                )
                if data["status"]:
                    self.api = new_api
                    self.auth_token = data["data"]["jwtToken"]
                    self.feed_token = new_api.getfeedToken()
                    self._heartbeat_failures = 0
                    self._last_connect_time = time.time()
                    # _connected stays True — no UI flicker
                    logger.info("Angel One: Token refreshed silently ✓")
                else:
                    logger.warning(f"Angel One: Token refresh failed: {data.get('message')}")
            except Exception as e:
                logger.warning(f"Angel One: Token refresh error: {e}")

    def _heartbeat_loop(self):
        while self._monitoring:
            try:
                if self._connected and self.api:
                    # Quick LTP check to verify session is still alive
                    result = self.api.ltpData("NSE", "Nifty 50", "99926000")
                    if result and result.get("status"):
                        self._heartbeat_failures = 0  # Reset on success
                    else:
                        msg = str(result.get("message", "") if result else "")
                        # Rate limit is NOT a real disconnect — skip counting
                        if "Access denied" in msg or "rate" in msg.lower():
                            logger.debug("Angel heartbeat: rate limited, not counting as failure")
                        else:
                            self._heartbeat_failures += 1
                            logger.warning(
                                f"Angel heartbeat failed ({self._heartbeat_failures}/5)"
                            )
                    # Only attempt reconnect after 5 consecutive real failures
                    if self._heartbeat_failures >= 5:
                        logger.warning("Angel heartbeat failed 5x, attempting reconnect...")
                        self._try_reconnect()
            except Exception as e:
                err_str = str(e)
                # Rate limit exceptions are not real disconnects
                if "Access denied" in err_str or "rate" in err_str.lower():
                    logger.debug(f"Heartbeat rate limit (not counted): {e}")
                else:
                    self._heartbeat_failures += 1
                    logger.debug(f"Heartbeat error ({self._heartbeat_failures}/5): {e}")
                    if self._heartbeat_failures >= 5:
                        logger.warning("Angel heartbeat errors repeated, attempting reconnect...")
                        self._try_reconnect()

            time.sleep(60)

    def is_connected(self) -> bool:
        """Returns the cached connection status (instant, non-blocking)"""
        return self._connected

    def _try_reconnect(self) -> bool:
        """Reconnect with exponential backoff — does NOT set _connected=False during attempts."""

        for attempt in range(3):
            try:
                self.api = SmartConnect(api_key=settings.ANGEL_API_KEY)
                totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
                data = self.api.generateSession(
                    settings.ANGEL_CLIENT_ID, settings.ANGEL_PASSWORD, totp
                )
                if data["status"]:
                    self.auth_token = data["data"]["jwtToken"]
                    self.feed_token = self.api.getfeedToken()
                    self._connected = True
                    self._heartbeat_failures = 0
                    logger.info(f"Angel One reconnected on attempt {attempt + 1}")
                    try:
                        from data.market_feed import market_feed

                        if market_feed._subscriptions:
                            market_feed.stop()
                            time.sleep(1)
                            market_feed.start()
                    except Exception as e:
                        logger.debug(f"Market feed restart skipped: {e}")
                    return True
                else:
                    logger.warning(
                        f"Reconnect attempt {attempt + 1} failed: {data.get('message')}"
                    )
            except Exception as e:
                logger.warning(f"Reconnect attempt {attempt + 1} error: {e}")

            time.sleep(2**attempt)  # 2, 4, 8 seconds

        # Only mark disconnected if ALL attempts failed
        self._connected = False
        self._heartbeat_failures = 0  # Reset so next heartbeat tries fresh
        logger.error("All reconnect attempts failed — marked as disconnected")
        return False

    def get_ltp(self, exchange: str, symbol: str, token: str) -> float | None:
        now = time.time()
        key = (exchange, token)
        
        with self._cache_lock:
            cached_val, ts = self._ltp_cache.get(key, (None, 0))
            if cached_val and (now - ts) < 1.0:  # 1 second cache
                return cached_val

        try:
            if not self._connected: return None
            data = self.api.ltpData(exchange, symbol, token)
            if data["status"]:
                ltp = float(data["data"]["ltp"])
                with self._cache_lock:
                    self._ltp_cache[key] = (ltp, now)
                return ltp
            
            # Handle rate limit error specifically
            if "Access denied" in str(data.get("message", "")):
                logger.warning(f"Angel Rate Limit hit for {symbol}. Using stale cache.")
                return cached_val
                
            return None
        except Exception as e:
            if "Access denied" in str(e):
                logger.warning(f"Angel Rate Limit hit (exception) for {symbol}. Using stale cache.")
                return cached_val
            logger.error(f"LTP fetch error for {symbol}: {e}")
            return None

    def get_quote(self, exchange: str, symbol: str, token: str) -> dict | None:
        try:
            data = self.api.getQuote(exchange, symbol, token)
            if data["status"]:
                return data["data"]
            return None
        except Exception as e:
            logger.error(f"Quote fetch error for {symbol}: {e}")
            return None

    def get_candle_data(
        self, token: str, exchange: str, interval: str, from_date: str, to_date: str
    ) -> list | None:
        """
        interval: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
                  FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY
        from_date / to_date: "YYYY-MM-DD HH:MM"
        """
        try:
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date,
            }
            data = self.api.getCandleData(params)
            if data["status"]:
                return data["data"]
            return None
        except Exception as e:
            logger.error(f"Candle data error for token {token}: {e}")
            return None

    def get_historical_data(
        self, token: str, exchange: str, interval: str, days: int
    ):
        """Fetch historical candles and return as a pandas DataFrame."""
        import pandas as pd
        from datetime import datetime, timedelta
        
        to_date   = datetime.now().strftime("%Y-%m-%d 23:59")
        from_date = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d 09:00")
        
        raw = self.get_candle_data(token, exchange, interval, from_date, to_date)
        if not raw:
            return pd.DataFrame()
            
        df = pd.DataFrame(raw, columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = df["date"].str[:10] # "YYYY-MM-DD"
        
        # Exclude today's incomplete candle
        today_str = datetime.now().strftime("%Y-%m-%d")
        df = df[df["date"] < today_str]
        
        return df.sort_values("date", ascending=False)

    def search_scrip(self, exchange: str, search_text: str) -> list:
        try:
            data = self.api.searchScrip(exchange, search_text)
            if data["status"]:
                return data["data"]
            return []
        except Exception as e:
            logger.error(f"Search scrip error: {e}")
            return []

    def get_token(self, exchange: str, symbol: str) -> str | None:
        """Get instrument token from symbol name with exact match preference"""
        results = self.search_scrip(exchange, symbol)
        if results:
            # Prefer exact symbol match first
            for res in results:
                if res.get("symbol") == symbol:
                    return res.get("symboltoken")
            # Fallback to first result
            return results[0].get("symboltoken")
        return None

    def get_current_future_symbol(self, instrument: str, exchange: str = "NFO", ref_date: datetime = None, allow_rollover: bool = True) -> dict | None:
        """Finds the current active future contract (Near or Next month) based on 10-day rollover rule."""
        try:
            from api.option_chain import load_master
            master = load_master()
            if not master: return None
        except Exception as e:
            logger.error(f"Master load failed in future search: {e}")
            return None

        candidates = []
        name_upper = instrument.upper()
        
        # Determine expected instrument type
        target_inst_type = "FUTIDX" if exchange == "NFO" else "FUTCOM"
        
        for row in master:
            if (row.get("name", "").upper() == name_upper and 
                row.get("exch_seg") == exchange and 
                row.get("instrumenttype") == target_inst_type):
                
                exp = row.get("expiry")
                if not exp: continue
                try:
                    # Parse DDMMMYYYY (e.g. 28APR2026)
                    exp_dt = datetime.strptime(exp, "%d%b%Y")
                    candidates.append({
                        "symbol": row["symbol"],
                        "token": row["token"],
                        "expiry": exp_dt,
                        "tradingsymbol": row["symbol"]
                    })
                except: continue

        # Filter candidates to only include those from the current or future years
        current_year = datetime.now().year
        candidates = [c for c in candidates if c["expiry"].year >= current_year]

        if not candidates:
            logger.error(f"No current or future {target_inst_type} found for {instrument}")
            return None

        candidates.sort(key=lambda x: x["expiry"])
        base_date = ref_date or datetime.now()
        
        # 10-Day Rollover Rule:
        # If nearest expiry is within 10 days, pick the next one if it exists.
        chosen = candidates[0]
        if len(candidates) > 1:
            # Filter for candidates expiring AFTER or ON base_date
            valid_candidates = [c for c in candidates if c["expiry"].date() >= base_date.date()]
            if not valid_candidates:
                return {"symbol": candidates[-1]["symbol"], "token": candidates[-1]["token"]}
                
            chosen = valid_candidates[0]
            if allow_rollover and len(valid_candidates) > 1:
                days_to_expiry = (valid_candidates[0]["expiry"] - base_date).days
                if days_to_expiry < 10:
                    chosen = valid_candidates[1]
                    logger.info(f"Rollover logic (as of {base_date.date()}): {instrument} near-month expiry in {days_to_expiry} days. Using {chosen['symbol']}.")
        
        return {"symbol": chosen["symbol"], "token": chosen["token"]}

    def disconnect(self):
        try:
            if self.api:
                self.api.terminateSession(settings.ANGEL_CLIENT_ID)
            self._monitoring = False
            self._connected = False
            logger.info("Angel One disconnected")
        except Exception as e:
            logger.error(f"Disconnect error: {e}")


# Singleton instance
angel_api = AngelOneAPI()
