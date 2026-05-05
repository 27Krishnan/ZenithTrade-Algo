"""
Option Chain API - Uses Angel One SmartAPI instrument master + batch LTP.
No NSE scraping needed. Fully authenticated, reliable data.

Flow:
1. Download Angel One instrument master JSON (cached daily, ~40MB)
2. Filter options for requested underlying + expiry
3. Batch-fetch LTPs via getMarketData (up to 50 tokens/call)
4. Return structured option chain
"""

import json
import time
import asyncio
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException
from loguru import logger

router = APIRouter(prefix="/api", tags=["option-chain"])

MASTER_URL  = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MASTER_FILE = Path("data_cache/instrument_master.json")
MASTER_TTL  = 86400  # refresh once per day
_master_cache: list | None = None

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 40,
    "MIDCPNIFTY": 75, "SENSEX": 10, "BANKEX": 15, "NIFTYIT": 35,
}


# ── Instrument Master ────────────────────────────────────────────────────────

def load_master(force: bool = False) -> list:
    global _master_cache
    if not force and _master_cache:
        return _master_cache

    MASTER_FILE.parent.mkdir(exist_ok=True)
    stale = not MASTER_FILE.exists() or (time.time() - MASTER_FILE.stat().st_mtime) > MASTER_TTL

    if stale or force:
        logger.info("Downloading instrument master from Angel One...")
        try:
            r = requests.get(MASTER_URL, timeout=60)
            data = r.json()
            with open(MASTER_FILE, "w") as f:
                json.dump(data, f)
            logger.info(f"Instrument master downloaded: {len(data)} instruments")
        except Exception as e:
            logger.error(f"Master download failed: {e}")
            if MASTER_FILE.exists():
                logger.info("Using cached master")
                with open(MASTER_FILE) as f:
                    data = json.load(f)
            else:
                return []
    else:
        with open(MASTER_FILE) as f:
            data = json.load(f)

    _master_cache = data
    return data


def _parse_expiry_date(exp: str):
    """Parse DDMMMYYYY to sortable date"""
    from datetime import datetime
    try:
        return datetime.strptime(exp, "%d%b%Y")
    except Exception:
        return datetime.max


def get_expiries(master: list, symbol: str) -> list[str]:
    """Get all available expiry dates for a symbol, sorted chronologically"""
    sym = symbol.upper()
    expiries = set()
    for inst in master:
        if (inst.get("name", "").upper() == sym and
                inst.get("instrumenttype", "") in ("OPTIDX", "OPTSTK") and
                inst.get("exch_seg", "") == "NFO"):
            exp = inst.get("expiry", "")
            if exp:
                expiries.add(exp)
    return sorted(expiries, key=_parse_expiry_date)


def get_options_for_expiry(master: list, symbol: str, expiry: str) -> list[dict]:
    """Get all option instruments for a symbol+expiry"""
    sym = symbol.upper()
    results = []
    for inst in master:
        if (inst.get("name", "").upper() == sym and
                inst.get("expiry", "") == expiry and
                inst.get("instrumenttype", "") in ("OPTIDX", "OPTSTK") and
                inst.get("exch_seg", "") == "NFO"):
            results.append(inst)
    return results


# ── Batch LTP Fetch ───────────────────────────────────────────────────────────

def fetch_ltps_batch(tokens: list[str], exchange: str = "NFO") -> dict[str, float]:
    """
    Fetch LTP for multiple tokens using Angel One getMarketData.
    Splits into chunks of 50 (API limit per call).
    Returns {token: ltp}
    """
    from data.angel_api import angel_api
    if not angel_api.is_connected():
        return {}

    ltp_map = {}
    chunk_size = 50
    chunks = [tokens[i:i+chunk_size] for i in range(0, len(tokens), chunk_size)]

    for chunk in chunks:
        try:
            result = angel_api.api.getMarketData("LTP", {exchange: chunk})
            if result and result.get("status"):
                fetched = result.get("data", {}).get("fetched", [])
                for item in fetched:
                    tok = item.get("symbolToken", "")
                    ltp = item.get("ltp", 0)
                    ltp_map[str(tok)] = float(ltp)
        except Exception as e:
            logger.error(f"Batch LTP error: {e}")
        time.sleep(0.1)  # small delay between chunks

    return ltp_map


def get_spot_price(symbol: str) -> float:
    """Get spot price of underlying index"""
    from data.angel_api import angel_api
    if not angel_api.is_connected():
        return 0.0

    index_tokens = {
        "NIFTY": ("NSE", "Nifty 50", "99926000"),
        "BANKNIFTY": ("NSE", "Nifty Bank", "99926009"),
        "FINNIFTY": ("NSE", "Nifty Fin Services", "99926037"),
        "MIDCPNIFTY": ("NSE", "NIFTY MID SELECT", "99926074"),
        "SENSEX": ("BSE", "SENSEX", "1"),
    }
    sym = symbol.upper()
    if sym in index_tokens:
        exch, name, token = index_tokens[sym]
        try:
            res = angel_api.api.ltpData(exch, name, token)
            if res and res.get("status"):
                return float(res["data"].get("ltp", 0))
        except Exception as e:
            logger.error(f"Spot price error for {symbol}: {e}")
    return 0.0


# ── Build Option Chain ────────────────────────────────────────────────────────

def build_chain(instruments: list[dict], ltp_map: dict[str, float], spot: float) -> list[dict]:
    """Build strike-indexed option chain from instruments + LTPs"""
    strikes: dict[float, dict] = {}
    for inst in instruments:
        strike = float(inst.get("strike", 0)) / 100.0  # Angel One stores strike * 100
        sym_upper = inst.get("symbol", "").upper()
        if sym_upper.endswith("CE"):
            itype = "CE"
        elif sym_upper.endswith("PE"):
            itype = "PE"
        else:
            continue # ignore non-expiry tokens if any
        
        token = str(inst.get("token", ""))
        ltp = ltp_map.get(token, 0.0)

        if strike not in strikes:
            strikes[strike] = {"strike": strike, "CE": None, "PE": None}
        strikes[strike][itype] = {
            "ltp": ltp,
            "token": token,
            "symbol": inst.get("symbol", ""),
            "lot_size": int(inst.get("lotsize", 1)),
            "oi": 0, "oi_change": 0, "volume": 0, "iv": 0,
            "bid": 0, "ask": 0, "pct_change": 0,
        }

    chain = sorted(strikes.values(), key=lambda x: x["strike"])
    # Find ATM
    if spot > 0:
        atm = min((s["strike"] for s in chain), key=lambda x: abs(x - spot), default=0)
    else:
        atm = chain[len(chain)//2]["strike"] if chain else 0
    return chain, atm


# ── API Endpoints ─────────────────────────────────────────────────────────────

@router.get("/option-chain/{symbol}")
async def get_option_chain(symbol: str, expiry: str = None):
    """Live option chain using Angel One instrument master + batch LTP"""
    symbol = symbol.upper().strip()
    from data.angel_api import angel_api
    if not angel_api.is_connected():
        raise HTTPException(status_code=503, detail="Angel One not connected")

    # Load instrument master (cached)
    loop = asyncio.get_running_loop()
    master = await loop.run_in_executor(None, load_master)
    if not master:
        raise HTTPException(status_code=503, detail="Instrument master not available")

    # Get available expiries
    expiries = get_expiries(master, symbol)
    if not expiries:
        raise HTTPException(status_code=404, detail=f"No options found for {symbol}")

    selected_expiry = expiry if expiry and expiry in expiries else expiries[0]

    # Get instruments for this expiry
    instruments = get_options_for_expiry(master, symbol, selected_expiry)
    if not instruments:
        raise HTTPException(status_code=404, detail=f"No options for {symbol} expiry {selected_expiry}")

    tokens = [str(inst["token"]) for inst in instruments]

    # Fetch LTPs and spot price concurrently
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ltp  = loop.run_in_executor(ex, fetch_ltps_batch, tokens, "NFO")
        f_spot = loop.run_in_executor(ex, get_spot_price, symbol)
        ltp_map, spot = await asyncio.gather(f_ltp, f_spot)

    chain, atm = build_chain(instruments, ltp_map, spot)
    # Use actual lot size from instrument master (most accurate)
    lot_size = LOT_SIZES.get(symbol, 1)
    if instruments:
        lot_size = int(instruments[0].get("lotsize", lot_size))

    logger.info(f"Option chain {symbol} {selected_expiry}: {len(chain)} strikes, spot={spot}")
    return {
        "symbol": symbol,
        "spot_price": spot,
        "expiry": selected_expiry,
        "expiries": expiries,
        "atm_strike": atm,
        "lot_size": lot_size,
        "chain": chain,
    }


@router.get("/option-chain/{symbol}/expiries")
async def get_expiry_list(symbol: str):
    """Get available expiry dates for a symbol"""
    loop = asyncio.get_running_loop()
    master = await loop.run_in_executor(None, load_master)
    return get_expiries(master, symbol.upper())


@router.post("/instrument-master/refresh")
async def refresh_master():
    """Force refresh the instrument master cache"""
    loop = asyncio.get_running_loop()
    master = await loop.run_in_executor(None, load_master, True)
    return {"status": "ok", "instruments": len(master)}


def get_fut_expiries(master: list, symbol: str) -> list[str]:
    """Get all available expiry dates for a future symbol"""
    sym = symbol.upper()
    expiries = set()
    for inst in master:
        if (inst.get("name", "").upper() == sym and
                inst.get("instrumenttype", "") in ("FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR")):
            exp = inst.get("expiry", "")
            if exp:
                expiries.add(exp)
    return sorted(expiries, key=_parse_expiry_date)


@router.get("/futures/{symbol}/expiries")
async def get_future_expiry_list(symbol: str):
    """Get available expiry dates for a future symbol"""
    loop = asyncio.get_running_loop()
    master = await loop.run_in_executor(None, load_master)
    if not master:
        raise HTTPException(status_code=503, detail="Master not loaded")
    return get_fut_expiries(master, symbol.upper())


@router.get("/futures/{symbol}/details")
async def get_future_details(symbol: str, expiry: str):
    """Get the specific instrument details for a future symbol + expiry"""
    loop = asyncio.get_running_loop()
    master = await loop.run_in_executor(None, load_master)
    sym = symbol.upper()
    for inst in master:
        if (inst.get("name", "").upper() == sym and
                inst.get("expiry", "") == expiry and
                inst.get("instrumenttype", "") in ("FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR")):
            return {
                "token": str(inst.get("token")),
                "symbol": inst.get("symbol"),
                "name": inst.get("name"),
                "exchange": inst.get("exch_seg"),
                "expiry": inst.get("expiry"),
                "lot_size": int(inst.get("lotsize", 1))
            }
    raise HTTPException(status_code=404, detail="Instrument not found")

from pydantic import BaseModel
class WatchlistPayload(BaseModel):
    items: list[dict]  # [{"token": "123", "exchange": "NFO"}, ...]

@router.post("/futures/quotes")
async def get_futures_quotes(payload: WatchlistPayload):
    """Fetch LTP for a custom list of future tokens"""
    from data.angel_api import angel_api
    if not angel_api.is_connected():
        return {}

    nfo_tokens = [item["token"] for item in payload.items if item.get("exchange") == "NFO"]
    mcx_tokens = [item["token"] for item in payload.items if item.get("exchange") == "MCX"]
    nse_tokens = [item["token"] for item in payload.items if item.get("exchange") == "NSE"]

    loop = asyncio.get_running_loop()
    ltp_map: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        tasks = []
        if nfo_tokens: tasks.append(loop.run_in_executor(ex, fetch_ltps_batch, nfo_tokens, "NFO"))
        if mcx_tokens: tasks.append(loop.run_in_executor(ex, fetch_ltps_batch, mcx_tokens, "MCX"))
        if nse_tokens: tasks.append(loop.run_in_executor(ex, fetch_ltps_batch, nse_tokens, "NSE"))
        
        if tasks:
            for r in await asyncio.gather(*tasks):
                ltp_map.update(r)

    return ltp_map


FUTURES_CONFIG = [
    # NSE Index Futures
    {"name": "NIFTY",       "exchange": "NFO", "itype": "FUTIDX", "display": "NIFTY FUT",       "category": "NSE"},
    {"name": "BANKNIFTY",   "exchange": "NFO", "itype": "FUTIDX", "display": "BANKNIFTY FUT",   "category": "NSE"},
    {"name": "FINNIFTY",    "exchange": "NFO", "itype": "FUTIDX", "display": "FINNIFTY FUT",    "category": "NSE"},
    {"name": "MIDCPNIFTY",  "exchange": "NFO", "itype": "FUTIDX", "display": "MIDCPNIFTY FUT",  "category": "NSE"},
    # MCX Gold
    {"name": "GOLD",        "exchange": "MCX", "itype": "FUTCOM", "display": "GOLD (1kg)",       "category": "MCX"},
    {"name": "GOLDM",       "exchange": "MCX", "itype": "FUTCOM", "display": "GOLDM (100g)",     "category": "MCX"},
    {"name": "GOLDPETAL",   "exchange": "MCX", "itype": "FUTCOM", "display": "GOLDPETAL (1g)",   "category": "MCX"},
    # MCX Silver
    {"name": "SILVER",      "exchange": "MCX", "itype": "FUTCOM", "display": "SILVER (30kg)",    "category": "MCX"},
    {"name": "SILVERM",     "exchange": "MCX", "itype": "FUTCOM", "display": "SILVERM (5kg)",    "category": "MCX"},
    {"name": "SILVERMIC",   "exchange": "MCX", "itype": "FUTCOM", "display": "SILVERMIC (1kg)",  "category": "MCX"},
    # MCX Energy
    {"name": "CRUDEOIL",    "exchange": "MCX", "itype": "FUTCOM", "display": "CRUDEOIL (100bbl)","category": "MCX"},
    {"name": "CRUDEOILM",   "exchange": "MCX", "itype": "FUTCOM", "display": "CRUDEOILM (10bbl)","category": "MCX"},
    {"name": "NATURALGAS",  "exchange": "MCX", "itype": "FUTCOM", "display": "NATURALGAS",       "category": "MCX"},
    {"name": "NATGASMINI",  "exchange": "MCX", "itype": "FUTCOM", "display": "NATGASMINI",       "category": "MCX"},
    # MCX Base Metals
    {"name": "COPPER",      "exchange": "MCX", "itype": "FUTCOM", "display": "COPPER (2500kg)",  "category": "MCX"},
    {"name": "ZINC",        "exchange": "MCX", "itype": "FUTCOM", "display": "ZINC (5MT)",       "category": "MCX"},
    {"name": "ALUMINIUM",   "exchange": "MCX", "itype": "FUTCOM", "display": "ALUMINIUM (5MT)",  "category": "MCX"},
    {"name": "ZINCMINI",    "exchange": "MCX", "itype": "FUTCOM", "display": "ZINCMINI (1MT)",   "category": "MCX"},
    {"name": "ALUMINI",     "exchange": "MCX", "itype": "FUTCOM", "display": "ALUMINI (1MT)",    "category": "MCX"},
]


def get_futures_instruments(master: list) -> list[dict]:
    """Find nearest expiry FUTCOM/FUTIDX future for each configured symbol"""
    results = []
    for cfg in FUTURES_CONFIG:
        candidates = [
            inst for inst in master
            if (inst.get("name", "").upper() == cfg["name"].upper() and
                inst.get("exch_seg", "") == cfg["exchange"] and
                inst.get("instrumenttype", "") == cfg["itype"] and  # exact match — avoids OPTFUT
                inst.get("expiry", ""))
        ]
        if candidates:
            candidates.sort(key=lambda x: _parse_expiry_date(x.get("expiry", "")))
            best = candidates[0]
            results.append({
                "cfg": cfg,
                "token": str(best.get("token", "")),
                "symbol": best.get("symbol", ""),
                "expiry": best.get("expiry", ""),
                "lot_size": int(best.get("lotsize", 1)),
            })
        else:
            results.append({"cfg": cfg, "token": None, "symbol": None, "expiry": None, "lot_size": 1})
    return results


@router.get("/futures")
async def get_futures():
    """Live LTP for all major futures (NSE index + MCX commodities)"""
    from data.angel_api import angel_api
    if not angel_api.is_connected():
        raise HTTPException(status_code=503, detail="Angel One not connected")

    loop = asyncio.get_running_loop()
    master = await loop.run_in_executor(None, load_master)
    if not master:
        raise HTTPException(status_code=503, detail="Instrument master not available")

    futures = get_futures_instruments(master)

    nfo_tokens = [f["token"] for f in futures if f["token"] and f["cfg"]["exchange"] == "NFO"]
    mcx_tokens = [f["token"] for f in futures if f["token"] and f["cfg"]["exchange"] == "MCX"]

    ltp_map: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        tasks = []
        if nfo_tokens:
            tasks.append(loop.run_in_executor(ex, fetch_ltps_batch, nfo_tokens, "NFO"))
        if mcx_tokens:
            tasks.append(loop.run_in_executor(ex, fetch_ltps_batch, mcx_tokens, "MCX"))
        if tasks:
            for r in await asyncio.gather(*tasks):
                ltp_map.update(r)

    output = []
    for f in futures:
        cfg = f["cfg"]
        ltp = ltp_map.get(f["token"], 0.0) if f["token"] else 0.0
        output.append({
            "name": cfg["name"],
            "display": cfg["display"],
            "category": cfg["category"],
            "exchange": cfg["exchange"],
            "symbol": f["symbol"] or cfg["name"],
            "expiry": f["expiry"],
            "token": f["token"],
            "lot_size": f["lot_size"],
            "ltp": ltp,
            "available": f["token"] is not None,
        })

    logger.info(f"Futures loaded: {len([x for x in output if x['ltp']>0])} with LTP")
    return output


@router.get("/instruments/search")
async def search_instruments(q: str = ""):
    """Returns a list of unique symbols for search dropdown."""
    try:
        loop = asyncio.get_running_loop()
        master = await loop.run_in_executor(None, load_master)
        if not master:
            return []
            
        # We use a set to get unique underlying symbols (name field in NFO)
        symbols_map = {}
        
        # Add common indices first
        indices = [
            {"symbol": "NIFTY", "name": "Nifty 50"},
            {"symbol": "BANKNIFTY", "name": "Bank Nifty"},
            {"symbol": "FINNIFTY", "name": "Fin Nifty"},
            {"symbol": "MIDCPNIFTY", "name": "Midcap Nifty"},
            {"symbol": "SENSEX", "name": "Sensex"},
        ]
        for idx in indices:
            symbols_map[idx["symbol"]] = idx["name"]

        for i in master:
            if i.get("exch_seg") == "NFO":
                sym = i.get("name", "")
                if sym and sym not in symbols_map:
                    # 'symbol' field usually has more info like 'RELIANCE28MAY26FUT'
                    # but 'name' is the underlying 'RELIANCE'
                    symbols_map[sym] = sym
        
        results = [{"symbol": k, "name": v} for k, v in symbols_map.items()]
        results.sort(key=lambda x: x["symbol"])
        
        if q:
            q = q.upper()
            return [i for i in results if q in i["symbol"] or q in i["name"].upper()]
        
        return results
    except Exception as e:
        logger.error(f"Error in search_instruments: {e}")
        return []

