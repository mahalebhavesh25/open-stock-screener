from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException

from app.core.config import CFG
from app.core.database import db, now_ist
from app.engine.broker import make_broker
from app.engine.data_hub import SYNTH, DataHub
from app.engine.llm_overlay import GeminiAnalyst
from app.engine.screener import run_screener
from app.engine.trade_planner import build_trade_plan
from app.engine.universe import build_universe

from pydantic import BaseModel

from app.tasks.task_manager import task_manager

router = APIRouter(prefix="/api/screener", tags=["screener"])
LOG = logging.getLogger("api.screener")


class ScreenerRunRequest(BaseModel):
    refresh: Optional[bool] = False
    use_llm: Optional[bool] = True
    save_signals: Optional[bool] = True


@router.post("/run")
def trigger_screener(
    payload: Optional[ScreenerRunRequest] = None,
    refresh: Optional[bool] = None,
    use_llm: Optional[bool] = None
) -> Dict[str, Any]:
    do_refresh = (payload.refresh if payload and payload.refresh is not None else None) if refresh is None else refresh
    if do_refresh is None:
        do_refresh = False
    do_use_llm = (payload.use_llm if payload and payload.use_llm is not None else None) if use_llm is None else use_llm
    if do_use_llm is None:
        do_use_llm = True

    task_id = task_manager.start_screener_task(refresh=do_refresh, use_llm=do_use_llm)
    return {
        "status": "started",
        "task_id": task_id,
        "message": "Screener scan initiated as background task with multi-stage tracking"
    }


def _enrich_signal_dict(s_dict: Dict[str, Any]) -> Dict[str, Any]:
    s_dict["signal_id"] = s_dict.get("id")
    s_dict["trade_id"] = s_dict.get("id")
    s_dict["entry_price"] = s_dict.get("entry_trigger")
    s_dict["stop_loss"] = s_dict.get("stop")
    s_dict["target_1"] = s_dict.get("t1")
    s_dict["target_2"] = s_dict.get("t2")

    entry = float(s_dict.get("entry_trigger") or 0.0)
    stop = float(s_dict.get("stop") or 0.0)
    t2 = float(s_dict.get("t2") or 0.0)
    risk = max(entry - stop, 0.01)
    reward = max(t2 - entry, 0.0)
    s_dict["reward_risk_ratio"] = round(reward / risk, 1) if risk > 0 else 2.0

    if isinstance(s_dict.get("features"), str):
        try:
            s_dict["features"] = json.loads(s_dict["features"])
        except Exception:
            s_dict["features"] = {}
    elif not s_dict.get("features"):
        s_dict["features"] = {}

    if isinstance(s_dict.get("llm"), str):
        try:
            s_dict["llm"] = json.loads(s_dict["llm"])
        except Exception:
            s_dict["llm"] = {}
    elif not s_dict.get("llm"):
        s_dict["llm"] = {}

    feat = s_dict["features"]
    llm = s_dict["llm"]

    s_dict["rsi_14"] = feat.get("rsi14") or feat.get("rsi_14")
    s_dict["adx_14"] = feat.get("adx") or feat.get("adx_14")
    s_dict["atr_14_pct"] = feat.get("atr_pct") or feat.get("atr_14_pct")
    s_dict["volume_ratio_20"] = feat.get("vol_ratio") or feat.get("volume_ratio_20")
    s_dict["dist_52w_high_pct"] = feat.get("dist_52w_high") or feat.get("dist_52w_high_pct")
    s_dict["llm_sanity_notes"] = llm.get("rationale") or llm.get("note") or feat.get("llm_note")
    s_dict["sma_20"] = feat.get("sma_20")
    s_dict["sma_50"] = feat.get("sma_50")
    s_dict["sma_200"] = feat.get("sma_200")
    return s_dict


@router.get("/latest")
def get_latest_recommendations() -> Dict[str, Any]:
    with db.get_connection() as conn:
        signals = conn.execute(
            """SELECT s1.* FROM signals s1
               INNER JOIN (SELECT symbol, MAX(created_at) as max_c FROM signals GROUP BY symbol) s2
               ON s1.symbol = s2.symbol AND s1.created_at = s2.max_c
               ORDER BY s1.score DESC LIMIT 50"""
        ).fetchall()
        last_scan = conn.execute(
            "SELECT * FROM scan_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    items = [_enrich_signal_dict(dict(s)) for s in signals]

    return {
        "last_scan": dict(last_scan) if last_scan else None,
        "signals": items,
    }


@router.get("/candidate/{symbol}")
def get_candidate_details(symbol: str) -> Dict[str, Any]:
    with db.get_connection() as conn:
        sig = conn.execute(
            "SELECT * FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
            (symbol.upper(),)
        ).fetchone()
        inst = conn.execute(
            "SELECT * FROM instruments WHERE symbol = ?",
            (symbol.upper(),)
        ).fetchone()
        candles = conn.execute(
            "SELECT close FROM candles WHERE symbol = ? ORDER BY date DESC LIMIT 200",
            (symbol.upper(),)
        ).fetchall()

    if not sig:
        raise HTTPException(status_code=404, detail=f"No recent signal for {symbol}")

    s_dict = _enrich_signal_dict(dict(sig))
    s_dict["instrument"] = dict(inst) if inst else {}

    if candles:
        closes = [float(c["close"]) for c in candles]
        if len(closes) >= 20 and not s_dict.get("sma_20"):
            s_dict["sma_20"] = round(sum(closes[:20]) / 20.0, 2)
        if len(closes) >= 50 and not s_dict.get("sma_50"):
            s_dict["sma_50"] = round(sum(closes[:50]) / 50.0, 2)
        if len(closes) >= 200 and not s_dict.get("sma_200"):
            s_dict["sma_200"] = round(sum(closes[:200]) / 200.0, 2)
        elif len(closes) > 0 and not s_dict.get("sma_200"):
            s_dict["sma_200"] = round(sum(closes) / len(closes), 2)

    return s_dict


