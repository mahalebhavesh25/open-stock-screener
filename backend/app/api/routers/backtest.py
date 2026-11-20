import logging
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel

from app.core.config import CFG
from app.core.database import db
from app.engine.backtester import Backtester, walk_forward
from app.engine.broker import make_broker
from app.engine.data_hub import SYNTH, DataHub
from app.engine.screener import compute_panel
from app.engine.universe import build_universe
from app.tasks.task_manager import task_manager

LOG_API = logging.getLogger("api.backtest")

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRunRequest(BaseModel):
    start_date: Optional[str] = "2023-01-01"
    end_date: Optional[str] = None
    initial_capital: Optional[float] = 1000000.0
    strategy: Optional[str] = "ALL"
    friction_model: Optional[bool] = True


class WalkForwardRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: Optional[float] = 1000000.0
    strategy: Optional[str] = "ALL"
    n_splits: Optional[int] = 6
    folds: Optional[int] = 6
    embargo_bars: Optional[int] = 10


@router.post("/run")
def run_quick_backtest(
    payload: Optional[BacktestRunRequest] = None,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
) -> Dict[str, Any]:
    req_start = (payload.start_date if payload and payload.start_date else None) or (start_date if isinstance(start_date, str) else None) or "2023-01-01"
    req_end = (payload.end_date if payload and payload.end_date else None) or (end_date if isinstance(end_date, str) else None)

    broker = make_broker(CFG)
    hub = DataHub(CFG, broker, SYNTH)

    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM instruments").fetchall()
        u_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if u_df.empty:
        u_df = build_universe(CFG, hub, broker, SYNTH, refresh=False)

    try:
        panel, bench_feat = compute_panel(CFG, hub, u_df, refresh=False)
        all_dates = sorted(set().union(*[set(f.index) for f in panel.values()]))
        if not all_dates:
            raise HTTPException(status_code=400, detail="No historical candles available for backtest.")

        min_date = all_dates[0]
        max_date = all_dates[-1]

        s_dt = pd.Timestamp(req_start)
        if s_dt < min_date or s_dt >= max_date:
            s_dt = min_date

        e_dt = pd.Timestamp(req_end) if req_end else max_date
        if e_dt > max_date or e_dt <= s_dt:
            e_dt = max_date

        strat = (payload.strategy if payload and payload.strategy else None) or "ALL"
        cap = payload.initial_capital if payload and payload.initial_capital else CFG.equity
        run_cfg = CFG
        if cap and cap > 0 and cap != CFG.equity:
            from dataclasses import replace
            run_cfg = replace(CFG, equity=cap)

        LOG_API.info("Executing Single-Pass Backtest [%s -> %s] (Strategy: %s, Capital: ₹%s)",
                     str(s_dt.date()), str(e_dt.date()), strat, f"{cap:,.0f}")
        bt = Backtester(run_cfg, panel, u_df, bench_feat, strategy=strat)
        results = bt.run(str(s_dt.date()), str(e_dt.date()), verbose=False)
        LOG_API.info("Single-Pass Backtest completed: %d trades simulated", len(results.get("trades", [])))
        return {"status": "completed", "results": results["metrics"], **results}
    except Exception as e:
        LOG_API.exception("Backtest run failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/walk-forward")
def trigger_walk_forward(
    payload: Optional[WalkForwardRequest] = None,
    folds: Optional[int] = Query(None)
) -> Dict[str, Any]:
    num_folds = (payload.folds or payload.n_splits if payload else None) or folds or 6
    broker = make_broker(CFG)
    hub = DataHub(CFG, broker, SYNTH)

    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM instruments").fetchall()
        u_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if u_df.empty:
        u_df = build_universe(CFG, hub, broker, SYNTH, refresh=False)

    start_dt = payload.start_date if payload else None
    end_dt = payload.end_date if payload else None
    strat = (payload.strategy if payload and payload.strategy else None) or "ALL"
    cap = payload.initial_capital if payload and payload.initial_capital else CFG.equity

    run_cfg = CFG
    from dataclasses import replace
    overrides = {}
    if cap and cap > 0 and cap != CFG.equity:
        overrides["equity"] = cap
    if payload and payload.embargo_bars and hasattr(CFG, "wf_embargo_bars"):
        overrides["wf_embargo_bars"] = payload.embargo_bars
    if overrides:
        run_cfg = replace(CFG, **overrides)

    try:
        LOG_API.info("Executing %d-Fold Purged Walk-Forward (Strategy: %s, Capital: ₹%s, Start: %s, End: %s)",
                     num_folds, strat, f"{run_cfg.equity:,.0f}", start_dt or "Window Start", end_dt or "Window End")
        panel, bench_feat = compute_panel(run_cfg, hub, u_df, refresh=False)
        fold_results = walk_forward(
            run_cfg, panel, u_df, bench_feat,
            folds=num_folds, variant="ANCHORED",
            start_date=start_dt, end_date=end_dt,
            strategy=strat
        )
        LOG_API.info("Purged Walk-Forward completed: %d folds evaluated", len(fold_results))
        return {
            "status": "completed",
            "results": {
                "folds": fold_results
            },
            "folds": fold_results
        }
    except Exception as e:
        LOG_API.exception("Walk-forward failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
