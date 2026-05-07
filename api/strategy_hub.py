from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.requests import Request
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates

from core.strategy_registry import strategy_registry


router = APIRouter()
templates = Jinja2Templates(directory="dashboard/templates")


class StrategyBacktestPayload(BaseModel):
    strategy: str
    instrument: str
    date: str


class SyncLivePayload(BaseModel):
    slug: str
    instrument: str
    type: str
    sim: dict


@router.get("/strategy-center", response_class=HTMLResponse)
async def strategy_center(request: Request):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("dashboard/templates"))
    template = env.get_template("strategy_center.html")
    html = template.render(request=request)
    return HTMLResponse(content=html)


@router.get("/api/strategy-hub/overview")
async def get_strategy_overview():
    return strategy_registry.overview()


@router.get("/api/strategy-hub/strategies")
async def get_strategy_list():
    data = []
    for strategy in strategy_registry.list():
        data.append(
            {
                "slug": strategy.slug,
                "name": strategy.name,
                "color": strategy.color,
                "instruments": strategy.instruments,
                "started": strategy.started,
                "error": strategy.start_error,
            }
        )
    return data


@router.get("/api/strategy-hub/history")
async def get_strategy_history(
    strategy: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
):
    return strategy_registry.history(slug=strategy, limit=limit)


@router.get("/api/strategy-hub/history-detail")
async def get_strategy_history_detail(
    strategy: str = Query(...),
    history_id: str = Query(..., alias="id"),
):
    try:
        return strategy_registry.get(strategy).history_detail(history_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategy-hub/backtest")
async def run_strategy_backtest(payload: StrategyBacktestPayload):
    try:
        strategy = strategy_registry.get(payload.strategy)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return strategy.run_backtest(payload.instrument, payload.date)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Backtest failed for {payload.strategy}/{payload.instrument}:\n{tb}")
        raise HTTPException(status_code=500, detail=tb)


@router.post("/api/strategy-hub/fetch/{strategy}")
async def trigger_strategy_fetch(strategy: str):
    try:
        runtime = strategy_registry.get(strategy)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    runtime.fetch_now()
    return {"success": True, "strategy": strategy}


@router.get("/api/strategy-hub/settings/{strategy}")
async def get_strategy_settings(strategy: str):
    try:
        runtime = strategy_registry.get(strategy)
        return runtime.get_settings()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategy-hub/settings/{strategy}")
async def save_strategy_settings(strategy: str, payload: dict):
    try:
        runtime = strategy_registry.get(strategy)
        return runtime.update_settings(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class SaveDefaultsPayload(BaseModel):
    auto_trade: bool
    levels: dict


@router.post("/api/strategy-hub/save-instrument-defaults")
async def save_instrument_defaults(
    strategy: str,
    instrument: str,
    payload: SaveDefaultsPayload
):
    try:
        runtime = strategy_registry.get(strategy)
        return runtime.save_instrument_defaults(instrument, payload.auto_trade, payload.levels)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategy-hub/sync-live")
async def run_sync_live(payload: SyncLivePayload):
    try:
        strategy = strategy_registry.get(payload.slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return strategy.sync_live(payload.instrument, payload.type, payload.sim)


class StateOverridePayload(BaseModel):
    slug: str           # e.g. "natural_gas"
    instrument: str     # e.g. "NATURALGAS"
    state: dict         # e.g. {"long_state": "PENDING", "short_state": "ACTIVE_P2", ...}


@router.post("/api/strategy-hub/override-state")
async def override_instrument_state(payload: StateOverridePayload):
    """Directly overwrite the live in-memory state for an instrument (admin use)."""
    try:
        strategy = strategy_registry.get(payload.slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    monitor = strategy._monitor_mod  # the monitor module
    from natural_gas_strategy.database import upsert_state as ng_upsert
    from gold_strategy.database import upsert_state as gold_upsert
    from silver_strategy.database import upsert_state as silver_upsert
    from nifty_strategy.database import upsert_state as nifty_upsert

    # Update DB first
    upsert_fns = {
        "natural-gas": ng_upsert,
        "gold": gold_upsert,
        "silver": silver_upsert,
        "nifty": nifty_upsert,
    }
    if payload.slug in upsert_fns:
        upsert_fns[payload.slug](payload.instrument, payload.state)

    # Now patch in-memory live state directly
    if hasattr(monitor, "_live") and hasattr(monitor, "_lock"):
        with monitor._lock:
            if payload.instrument not in monitor._live:
                monitor._live[payload.instrument] = {}
            monitor._live[payload.instrument].update(payload.state)
        return {"ok": True, "message": f"{payload.instrument} state overridden in memory and DB"}
    else:
        return {"ok": False, "message": "Monitor does not expose _live/_lock — DB updated only"}
