from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel

from app.core.database import db
from app.engine.data_hub import load_candles_from_db
from app.tasks.task_manager import task_manager

router = APIRouter(prefix="/api/data", tags=["data"])


class BackfillRequest(BaseModel):
    symbols: Optional[List[str]] = None
    lookback_bars: Optional[int] = 400
    lookback_days: Optional[int] = None


@router.get("/stats")
def get_data_stats() -> Dict[str, Any]:
    with db.get_connection() as conn:
        c_count = conn.execute("SELECT count(*) FROM candles").fetchone()[0]
        s_count = conn.execute("SELECT count(DISTINCT symbol) FROM candles").fetchone()[0]
        date_span = conn.execute("SELECT min(date), max(date) FROM candles").fetchone()
        symbols_summary = conn.execute(
            "SELECT symbol, count(*) as bars, min(date) as first_date, max(date) as last_date "
            "FROM candles GROUP BY symbol ORDER BY bars DESC LIMIT 50"
        ).fetchall()

    return {
        "total_candles": c_count,
        "total_symbols": s_count,
        "earliest_date": date_span[0] if date_span else None,
        "latest_date": date_span[1] if date_span else None,
        "top_cached_symbols": [dict(r) for r in symbols_summary],
    }


@router.get("/candles/{symbol}")
def get_candles(
    symbol: str,
    start_date: Optional[str] = Query(None),
    limit: int = Query(300, ge=10, le=2000)
) -> List[Dict[str, Any]]:
    df = load_candles_from_db(symbol.upper(), start_date)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No candles cached for {symbol}")
    df = df.tail(limit)
    records = []
    for dt, r in df.iterrows():
        records.append({
            "date": dt.strftime("%Y-%m-%d"),
            "open": round(float(r["open"]), 2),
            "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2),
            "close": round(float(r["close"]), 2),
            "volume": int(r.get("volume", 0)),
        })
    return records


@router.post("/backfill")
def start_backfill(
    payload: Optional[BackfillRequest] = None,
    lookback_days: Optional[int] = Query(None)
) -> Dict[str, Any]:
    symbols = payload.symbols if payload and payload.symbols else None
    if payload and payload.lookback_days:
        days = payload.lookback_days
    elif payload and payload.lookback_bars:
        days = int(payload.lookback_bars * 1.5)
    elif lookback_days:
        days = lookback_days
    else:
        days = 600

    task_id = task_manager.start_data_backfill_task(symbols=symbols, lookback_days=days)
    return {
        "status": "started",
        "task_id": task_id,
        "message": f"Historical candle backfill ({days} days / ~{payload.lookback_bars if payload else 400} bars) initiated"
    }
