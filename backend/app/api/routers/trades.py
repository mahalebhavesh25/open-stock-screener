from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from app.core.config import CFG
from app.core.database import db, now_ist
from app.engine.broker import make_broker
from app.engine.lifecycle import LifecycleEngine, TState
from app.engine.trade_planner import TradePlan

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("/")
def get_active_trades(state: Optional[str] = None) -> Dict[str, Any]:
    with db.get_connection() as conn:
        if state == "PROPOSED":
            rows = conn.execute(
                """SELECT s1.id AS signal_id, s1.id AS trade_id, s1.symbol, s1.strategy, s1.score, 
                          s1.entry_trigger AS entry_price, s1.entry_trigger,
                          s1.stop AS stop_loss, s1.stop,
                          s1.t1 AS target_1, s1.t1,
                          s1.t2 AS target_2, s1.t2,
                          s1.qty, s1.risk_amount, s1.r_per_share, s1.status, s1.created_at
                   FROM signals s1
                   INNER JOIN (SELECT symbol, MAX(created_at) as max_c FROM signals WHERE status = 'PROPOSED' GROUP BY symbol) s2
                   ON s1.symbol = s2.symbol AND s1.created_at = s2.max_c
                   WHERE s1.status = 'PROPOSED'
                   ORDER BY s1.score DESC"""
            ).fetchall()
        elif state:
            rows = conn.execute(
                "SELECT * FROM trades WHERE state = ? ORDER BY created_at DESC", (state,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE state NOT IN ('CLOSED', 'CANCELLED', 'EXPIRED', 'REJECTED') ORDER BY created_at DESC"
            ).fetchall()
        trades = [dict(r) for r in rows]
        return {"trades": trades, "count": len(trades)}


@router.get("/history")
def get_closed_trades(limit: int = 50) -> Dict[str, Any]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE state IN ('CLOSED', 'CANCELLED', 'EXPIRED', 'REJECTED') ORDER BY updated_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        history = [dict(r) for r in rows]
        return {"history": history, "count": len(history)}


@router.get("/orders")
def get_orders_log(limit: int = 50) -> Dict[str, Any]:
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        orders = [dict(r) for r in rows]
        return {"orders": orders, "count": len(orders)}



@router.post("/approve/{signal_id}")
def approve_trade(signal_id: str) -> Dict[str, Any]:
    with db.get_connection() as conn:
        sig = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not sig:
            raise HTTPException(status_code=404, detail="Signal not found")
        sig_dict = dict(sig)
        entry_trigger = float(sig_dict["entry_trigger"])
        init_stop = float(sig_dict["stop"])
        r_per_share = float(sig_dict.get("r_per_share") or (entry_trigger - init_stop))
        stop_pct = round(abs(entry_trigger - init_stop) / entry_trigger * 100, 1) if entry_trigger > 0 else 8.0

        plan = TradePlan(
            id=sig_dict["id"],
            scan_date=sig_dict["scan_date"],
            symbol=sig_dict["symbol"],
            name=sig_dict["symbol"],
            sector="UNKNOWN",
            strategy=sig_dict["strategy"],
            score=float(sig_dict["score"]),
            entry_type=sig_dict["entry_type"],
            entry_trigger=entry_trigger,
            valid_till=now_ist().date().isoformat(),
            init_stop=init_stop,
            r_per_share=r_per_share,
            t1=float(sig_dict["t1"]),
            t2=float(sig_dict["t2"]),
            qty=int(sig_dict["qty"]),
            risk_amount=float(sig_dict["risk_amount"]),
            notional=float(sig_dict["qty"] * entry_trigger),
            risk_pct_equity=round(float(sig_dict["risk_amount"]) / max(CFG.equity, 1.0) * 100, 2),
            stop_pct=stop_pct,
            trail_rule="chandelier",
            time_stop_bars=12,
            regime=sig_dict.get("regime", "NEUTRAL"),
            binding_cap="risk"
        )

        notifier = None
        try:
            from app.engine.telegram import TelegramNotifier
            notifier = TelegramNotifier(CFG)
        except Exception:
            pass

        broker = make_broker(CFG)
        engine = LifecycleEngine(CFG, broker, notifier=notifier)
        submitted = engine.submit_entry(plan)

        conn.execute("UPDATE signals SET status = 'APPROVED' WHERE id = ?", (signal_id,))

        if notifier:
            notifier.notify_trade_approved(plan)

        return {"status": "submitted" if submitted else "blocked", "signal_id": signal_id}



@router.post("/reject/{signal_id}")
def reject_trade(signal_id: str) -> Dict[str, Any]:
    with db.get_connection() as conn:
        sig = conn.execute("SELECT symbol FROM signals WHERE id = ?", (signal_id,)).fetchone()
        sym = sig["symbol"] if sig else signal_id
        conn.execute("UPDATE signals SET status = 'REJECTED' WHERE id = ?", (signal_id,))
        conn.execute("UPDATE trades SET state = 'REJECTED' WHERE id = ?", (signal_id,))

        try:
            from app.engine.telegram import TelegramNotifier
            notifier = TelegramNotifier(CFG)
            notifier.notify_trade_rejected(sym, "Rejected by user from dashboard")
        except Exception:
            pass

    return {"status": "rejected", "signal_id": signal_id}



@router.post("/close/{trade_id}")
def force_close_trade(trade_id: str) -> Dict[str, Any]:
    with db.get_connection() as conn:
        t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Trade not found")
        t_dict = dict(t)
        open_qty = int(t_dict.get("open_qty") or 0)
        symbol = t_dict["symbol"]

        broker = make_broker(CFG)
        # Fetch current quote
        q = broker.quote(symbol)
        ltp = float(q.get("ltp") or t_dict.get("avg_entry", 100))
        realized_pnl = round((ltp - float(t_dict.get("avg_entry", 0))) * open_qty, 2)

        conn.execute(
            "UPDATE trades SET state = 'CLOSED', open_qty = 0, closed_at = ?, exit_reason = 'MANUAL_EXIT', "
            "realized_pnl = ? WHERE id = ?",
            (now_ist().isoformat(), realized_pnl, trade_id)
        )
        db.log_event("FORCE_CLOSE", {"trade_id": trade_id, "symbol": symbol, "pnl": realized_pnl})
    return {"status": "closed", "trade_id": trade_id, "realized_pnl": realized_pnl}
