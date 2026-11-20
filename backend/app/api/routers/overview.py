from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List

from fastapi import APIRouter

from app.core.config import CFG
from app.core.database import db
from app.engine.calendar import get_market_status
from app.tasks.task_manager import task_manager

router = APIRouter(prefix="/api/overview", tags=["overview"])
LOG = logging.getLogger("api.overview")

# In-memory regime cache to guarantee zero blocking on GET /api/overview/
_regime_lock = threading.Lock()
_regime_cache: Dict[str, Any] = {
    "cached_at": 0.0,
    "data": {
        "status": "NEUTRAL",
        "state": "NEUTRAL",
        "exposure_multiplier": 0.70,
        "exposure_mult": 0.70,
        "benchmark_symbol": CFG.benchmark,
        "benchmark_close": 18340.0,
        "above_200_dma": True,
        "breadth_pct": 52.0,
        "breadth_above_50_dma": True,
        "bench_above_50": True,
        "bench_above_200": True,
        "bench_dd": 0.0,
        "max_new": CFG.max_new_per_day,
        "note": "Regime estimated (default operational posture)"
    }
}
_refreshing_regime = False


def _refresh_regime_background() -> None:
    global _refreshing_regime
    if _refreshing_regime:
        return
    _refreshing_regime = True

    def _worker():
        global _refreshing_regime
        try:
            from app.engine.broker import make_broker
            from app.engine.data_hub import SYNTH, DataHub
            from app.engine.features import compute_features
            from app.engine.regime import assess_regime

            broker = make_broker(CFG)
            hub = DataHub(CFG, broker, SYNTH)
            bench_df = hub.benchmark(lookback_days=400)
            if not bench_df.empty and len(bench_df) >= 50:
                bf = compute_features(bench_df)
                regime = assess_regime(bf, 50.0, CFG, verbose=False)
                rd = regime.to_dict()
                status = rd.get("state", "NEUTRAL")
                mult = rd.get("exposure_mult", 0.70)
                above_200 = rd.get("bench_above_200", True)
                above_50 = rd.get("bench_above_50", True)
                breadth = rd.get("breadth", 50.0)
                last_close = float(bench_df["close"].iloc[-1]) if "close" in bench_df else 18340.0

                with _regime_lock:
                    _regime_cache["cached_at"] = time.time()
                    _regime_cache["data"] = {
                        "status": status,
                        "state": status,
                        "exposure_multiplier": mult,
                        "exposure_mult": mult,
                        "benchmark_symbol": CFG.benchmark,
                        "benchmark_close": last_close,
                        "above_200_dma": above_200,
                        "breadth_pct": breadth,
                        "breadth_above_50_dma": above_50,
                        "bench_above_50": above_50,
                        "bench_above_200": above_200,
                        "bench_dd": rd.get("bench_dd", 0.0),
                        "max_new": rd.get("max_new", CFG.max_new_per_day),
                        "note": rd.get("note", "Live evaluated regime")
                    }
        except Exception as e:
            LOG.warning("Background regime evaluation skipped: %s", e)
            with _regime_lock:
                # Back off on failure so frontend polling does not loop endlessly (retry in 180s)
                _regime_cache["cached_at"] = time.time() - 120.0
        finally:
            _refreshing_regime = False

    t = threading.Thread(target=_worker, daemon=True, name="bg-regime-eval")
    t.start()


@router.get("/")
def get_overview() -> Dict[str, Any]:
    market = get_market_status()
    db_stats = db.get_stats()

    # Trigger async refresh if cache is older than 5 minutes
    if time.time() - _regime_cache["cached_at"] > 300.0 and not _refreshing_regime:
        _refresh_regime_background()

    with _regime_lock:
        regime_data = dict(_regime_cache["data"])

    # Calculate portfolio equity summary directly from SQLite
    with db.get_connection() as conn:
        open_trades = conn.execute(
            "SELECT count(*) as count, coalesce(sum(open_qty * avg_entry), 0.0) as deployed FROM trades "
            "WHERE state NOT IN ('CLOSED', 'CANCELLED', 'EXPIRED', 'REJECTED')"
        ).fetchone()
        closed_row = conn.execute(
            "SELECT count(*) as total_count, coalesce(sum(realized_pnl), 0.0) as pnl FROM trades WHERE state = 'CLOSED'"
        ).fetchone()
        closed_pnl = float(closed_row["pnl"] if closed_row else 0.0)
        total_trades_count = int(closed_row["total_count"] if closed_row else 0)

    deployed = float(open_trades["deployed"] if open_trades else 0.0)
    cash = max(CFG.equity - deployed + closed_pnl, 0.0)
    total_equity = cash + deployed
    peak_equity = max(CFG.equity, total_equity)
    drawdown_pct = round((total_equity / peak_equity - 1) * 100, 2) if peak_equity > 0 else 0.0
    open_count = int(open_trades["count"] if open_trades else 0)

    # Active running tasks for immediate UI sync
    all_tasks = task_manager.list_tasks(limit=10)
    active_tasks = [t for t in all_tasks if t.get("status") in ("RUNNING", "PAUSED")]

    portfolio_summary = {
        "initial_capital": CFG.equity,
        "initial_equity": CFG.equity,
        "cash": round(cash, 2),
        "deployed": round(deployed, 2),
        "capital_deployed": round(deployed, 2),
        "total_equity": round(total_equity, 2),
        "peak_equity": round(peak_equity, 2),
        "drawdown_pct": drawdown_pct,
        "realized_pnl": round(closed_pnl, 2),
        "open_positions_count": open_count,
        "total_trades_count": total_trades_count,
    }

    return {
        "run_mode": CFG.run_mode,
        "execution_mode": CFG.mode,
        "dry_run": CFG.dry_run,
        "data_source": CFG.data_source,
        "market": market,
        "portfolio": portfolio_summary,
        "capital": portfolio_summary,
        "open_positions_count": open_count,
        "regime": regime_data,
        "database": db_stats,
        "db_stats": db_stats,
        "active_tasks": active_tasks,
    }
