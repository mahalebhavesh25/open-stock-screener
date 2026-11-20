from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from app.core.config import CFG
from app.core.database import db
from app.engine.broker import GrowwBroker, make_broker
from app.engine.data_hub import SYNTH, DataHub
from app.tasks.task_manager import task_manager

router = APIRouter(prefix="/api/universe", tags=["universe"])
LOG = logging.getLogger("api.universe")


@router.get("/")
def get_universe(
    search: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    limit: Optional[int] = Query(None)
) -> Dict[str, Any]:
    if limit is not None:
        page_size = limit
    offset = (page - 1) * page_size

    query = "SELECT * FROM instruments WHERE 1=1"
    count_query = "SELECT count(*) FROM instruments WHERE 1=1"
    params = []
    if search:
        clause = " AND (symbol LIKE ? OR name LIKE ?)"
        query += clause
        count_query += clause
        params.extend([f"%{search}%", f"%{search}%"])
    if sector:
        query += " AND sector = ?"
        count_query += " AND sector = ?"
        params.append(sector)

    with db.get_connection() as conn:
        total = conn.execute(count_query, params).fetchone()[0]
        query += " ORDER BY mcap_cr DESC LIMIT ? OFFSET ?"
        rows = conn.execute(query, params + [page_size, offset]).fetchall()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "instruments": [dict(r) for r in rows]
        }



@router.post("/build")
def trigger_universe_build(refresh: bool = True) -> Dict[str, Any]:
    task_id = task_manager.start_universe_build_task(refresh=refresh)
    return {"status": "started", "task_id": task_id, "message": "Universe build started as background task"}


@router.get("/preflight")
def run_preflight() -> Dict[str, Any]:
    broker = make_broker(CFG)
    hub = DataHub(CFG, broker, SYNTH)
    bench_symbol_res = hub.resolve_benchmark_symbol()

    probes = {
        "run_mode": CFG.run_mode,
        "broker": broker.name,
        "data_source": CFG.data_source,
        "groww_authenticated": isinstance(broker, GrowwBroker) and broker._token_valid(),
        "benchmark": CFG.benchmark,
        "benchmark_resolved": bench_symbol_res is not None,
        "benchmark_resolution": bench_symbol_res,
        "synthetic_fallback": CFG.allow_synthetic_fallback,
        "equity_setting": CFG.equity,
        "risk_per_trade_pct": CFG.risk_per_trade_pct,
        "max_stop_pct": CFG.max_stop_pct,
    }
    return probes
