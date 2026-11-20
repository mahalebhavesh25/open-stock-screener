from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.core.config import Config
from app.core.database import db, now_ist
from app.engine.broker import BrokerAdapter, OrdStatus, OrdType, StrEnum, Txn
from app.engine.frictions import transaction_costs
from app.engine.trade_planner import TradePlan

LOG_LC = logging.getLogger("engine.lifecycle")


class TState(StrEnum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ENTRY_WORKING = "ENTRY_WORKING"
    OPEN = "OPEN"
    SCALED_OUT = "SCALED_OUT"
    TRAILING = "TRAILING"
    EXIT_WORKING = "EXIT_WORKING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass
class Position:
    """Live view of one open trade. Mirrors the `trades` table."""
    id: str
    symbol: str
    strategy: str
    qty: int                       # open quantity
    init_qty: int
    avg_entry: float
    init_stop: float
    cur_stop: float
    t1: float
    t2: float
    r: float                       # ₹ risk per share at entry
    entry_date: str
    bars_held: int = 0
    scaled: bool = False
    be_moved: bool = False
    high_water: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    state: str = TState.OPEN
    sector: str = "UNKNOWN"

    def unrealised_r(self, px: float) -> float:
        return (px - self.avg_entry) / self.r if self.r else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "symbol": self.symbol, "strategy": self.strategy,
            "qty": self.qty, "init_qty": self.init_qty, "avg_entry": self.avg_entry,
            "init_stop": self.init_stop, "cur_stop": self.cur_stop, "t1": self.t1,
            "t2": self.t2, "r": self.r, "entry_date": self.entry_date,
            "bars_held": self.bars_held, "scaled": self.scaled, "be_moved": self.be_moved,
            "high_water": self.high_water, "mfe_r": self.mfe_r, "mae_r": self.mae_r,
            "state": self.state, "sector": self.sector,
        }


@dataclass
class Action:
    kind: str          # EXIT_ALL | SCALE_OUT | MOVE_STOP
    qty: int
    price: float
    reason: str


def exit_decisions(p: Position, bar: Dict[str, float], atr: float,
                   cfg: Config, regime_state: str = "NEUTRAL") -> List[Action]:
    """Pure. Given a position and one daily bar, return the actions to take, in execution order."""
    acts: List[Action] = []
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

    # 1. Protective stop
    if bool(bar.get("lower_circuit_locked", False)):
        return []
    if o <= p.cur_stop:
        return [Action("EXIT_ALL", p.qty, o, "STOP_GAP")]
    if l <= p.cur_stop:
        return [Action("EXIT_ALL", p.qty, p.cur_stop, "STOP")]

    # 2. Targets
    if not p.scaled and h >= p.t1 and p.qty >= 1:
        if cfg.t1_exit_fraction >= 0.99 or p.qty == 1:
            acts.append(Action("EXIT_ALL", p.qty, max(p.t1, o), "T1"))
            return acts
        else:
            q = max(1, int(round(p.qty * cfg.t1_exit_fraction)))
            acts.append(Action("SCALE_OUT", q, max(p.t1, o), "T1"))
            fee_buf = max(0.10 * p.r, 0.003 * p.avg_entry)
            acts.append(Action("MOVE_STOP", 0, round(p.avg_entry + fee_buf, 2), "BREAKEVEN_AFTER_T1"))
    if p.scaled and h >= p.t2:
        return acts + [Action("EXIT_ALL", p.qty, max(p.t2, o), "T2")]

    # 3. Follow-through, time & regime stops
    r_now = p.unrealised_r(c)
    if (cfg.use_follow_through_exit and not p.scaled and
            p.bars_held >= cfg.follow_through_bars and
            p.mfe_r < cfg.follow_through_min_r and
            r_now < cfg.follow_through_min_r and
            p.mfe_r < cfg.keep_right_tail_after_r):
        return acts + [Action("EXIT_ALL", p.qty, c, "NO_FOLLOW_THROUGH")]
    if p.bars_held >= cfg.time_stop_bars and r_now < 0.8 and not p.scaled:
        return acts + [Action("EXIT_ALL", p.qty, c, "TIME_STOP")]
    if p.bars_held >= cfg.max_hold_bars:
        return acts + [Action("EXIT_ALL", p.qty, c, "MAX_HOLD")]
    if regime_state == "RISK_OFF" and r_now <= 0.0 and p.bars_held >= 3:
        return acts + [Action("EXIT_ALL", p.qty, c, "REGIME_EXIT")]

    # 4. Trail & Breakeven Lock
    new_stop = p.cur_stop
    # If trade reached breakeven trigger (either intraday MFE or close), lock in fees + small profit
    if (r_now >= cfg.breakeven_after_r or p.mfe_r >= cfg.breakeven_after_r) and not p.be_moved:
        fee_buf = max(0.10 * p.r, 0.003 * p.avg_entry)
        new_stop = max(new_stop, p.avg_entry + fee_buf)
    if r_now >= cfg.trail_activate_r and np.isfinite(atr) and atr > 0:
        chandelier = max(h, p.high_water, c) - cfg.chandelier_mult * atr
        new_stop = max(new_stop, chandelier)
    new_stop = min(new_stop, c * 0.995)
    if new_stop > p.cur_stop + 1e-6:
        acts.append(Action("MOVE_STOP", 0, round(new_stop, 2), "TRAIL"))
    return acts


class LifecycleEngine:
    def __init__(self, cfg: Config, broker: BrokerAdapter, notifier=None):
        self.cfg, self.broker, self.notifier = cfg, broker, notifier
        self.positions: Dict[str, Position] = {}
        self.pending: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    def _load_state(self) -> None:
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE state NOT IN ('CLOSED','CANCELLED','EXPIRED','REJECTED')"
            ).fetchall()
        for r in rows:
            t = dict(r)
            if t["state"] in (TState.OPEN, TState.SCALED_OUT, TState.TRAILING):
                self.positions[t["id"]] = Position(
                    id=t["id"], symbol=t["symbol"], strategy=t["strategy"],
                    qty=int(t["open_qty"] or 0), init_qty=int(t["filled_qty"] or 0),
                    avg_entry=float(t["avg_entry"] or 0), init_stop=float(t["init_stop"] or 0),
                    cur_stop=float(t["cur_stop"] or 0), t1=float(t["t1"] or 0),
                    t2=float(t["t2"] or 0), r=float(t["r_per_share"] or 1),
                    entry_date=str(t["created_at"]), bars_held=0,
                    scaled=t["state"] == TState.SCALED_OUT, state=t["state"]
                )
        LOG_LC.info("restored %d open positions from database", len(self.positions))

    def _persist_trade(self, t: Dict[str, Any]) -> None:
        cols = ["id", "signal_id", "symbol", "strategy", "state", "side", "planned_qty",
                "filled_qty", "open_qty", "avg_entry", "entry_trigger", "init_stop", "cur_stop",
                "t1", "t2", "realized_pnl", "max_favorable_excursion", "max_adverse_excursion",
                "created_at", "closed_at", "exit_reason"]
        filtered = {k: t.get(k) for k in cols if k in t}
        placeholders = ", ".join(["?"] * len(filtered))
        col_names = ", ".join(filtered.keys())
        update_clause = ", ".join([f"{k}=excluded.{k}" for k in filtered.keys() if k != "id"])
        with db.get_connection() as conn:
            conn.execute(
                f"INSERT INTO trades ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {update_clause}",
                list(filtered.values())
            )

    def _persist_position(self, p: Position, **extra) -> None:
        trade_dict = {
            "id": p.id, "signal_id": p.id, "symbol": p.symbol, "strategy": p.strategy,
            "state": p.state, "side": "LONG", "filled_qty": p.init_qty, "open_qty": p.qty,
            "avg_entry": p.avg_entry, "init_stop": p.init_stop, "cur_stop": p.cur_stop,
            "t1": p.t1, "t2": p.t2, "max_favorable_excursion": p.mfe_r,
            "max_adverse_excursion": p.mae_r, "created_at": p.entry_date,
            **extra
        }
        self._persist_trade(trade_dict)

    def _log_order(self, trade_id: str, resp: Dict[str, Any], symbol: str, txn: str,
                   qty: int, order_type: str, price: float = 0, trigger: float = 0) -> str:
        oid = resp.get("order_id", f"ORD-{uuid.uuid4().hex[:10]}")
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO orders (id, trade_id, symbol, broker_order_id, side, order_type, qty, "
                "filled_qty, price, trigger_price, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"ORD-{uuid.uuid4().hex[:10]}", trade_id, symbol, oid, str(txn), str(order_type),
                 qty, 0, price, trigger, str(resp.get("status", "NEW")),
                 now_ist().isoformat(), now_ist().isoformat())
            )
        return oid

    def portfolio_open_risk(self) -> float:
        return sum(max(p.avg_entry - p.cur_stop, 0) * p.qty for p in self.positions.values())

    def sector_count(self, sector: str) -> int:
        return sum(1 for p in self.positions.values() if p.sector == sector)

    def can_open(self, plan: TradePlan) -> Tuple[bool, str]:
        if len(self.positions) >= self.cfg.max_positions:
            return False, "max positions reached"
        if any(p.symbol == plan.symbol for p in self.positions.values()):
            return False, "already long"
        if plan.id in self.pending:
            return False, "entry already working"
        if self.sector_count(plan.sector) >= self.cfg.max_sector_positions:
            return False, f"sector cap ({plan.sector})"
        if db.get_setting("halted", False):
            return False, "engine halted by kill switch"
        return True, ""

    def propose(self, plan: TradePlan) -> str:
        ok, why = self.can_open(plan)
        state = TState.PROPOSED if ok else TState.REJECTED
        if ok and self.cfg.require_approval and self.cfg.mode == "LIVE":
            state = TState.PENDING_APPROVAL
        row = plan.to_row()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO signals (id, scan_date, symbol, strategy, score, entry_type, entry_trigger, "
                "stop, t1, t2, qty, risk_amount, r_per_share, regime, features, llm, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["scan_date"], row["symbol"], row["strategy"], row["score"],
                 row["entry_type"], row["entry_trigger"], row["stop"], row["t1"], row["t2"],
                 row["qty"], row["risk_amount"], row["r_per_share"], row["regime"],
                 row["features"], row["llm"], state, row["created_at"])
            )
        self._persist_trade({
            "id": plan.id, "signal_id": plan.id, "symbol": plan.symbol, "strategy": plan.strategy,
            "state": state, "side": "LONG", "planned_qty": plan.qty, "filled_qty": 0,
            "open_qty": 0, "avg_entry": 0.0, "entry_trigger": plan.entry_trigger,
            "init_stop": plan.init_stop, "cur_stop": plan.init_stop, "t1": plan.t1, "t2": plan.t2,
            "created_at": now_ist().isoformat()
        })
        db.log_event("PROPOSE", {"symbol": plan.symbol, "strategy": plan.strategy, "score": plan.score, "state": state})
        return state

    def submit_entry(self, plan: TradePlan) -> bool:
        ok, why = self.can_open(plan)
        if not ok:
            LOG_LC.info("entry blocked %s: %s", plan.symbol, why)
            return False
        otype = OrdType.SL_M if plan.entry_type == "BUY_STOP" else OrdType.LIMIT
        resp = self.broker.place_order(
            symbol=plan.symbol, txn=Txn.BUY, qty=plan.qty, order_type=otype,
            price=plan.entry_trigger if otype == OrdType.LIMIT else 0.0,
            trigger_price=plan.entry_trigger if otype == OrdType.SL_M else 0.0,
            ref_id=plan.id.replace("SIG-", "E"))
        oid = self._log_order(plan.id, resp, plan.symbol, Txn.BUY, plan.qty, otype,
                              plan.entry_trigger, plan.entry_trigger)
        self.pending[plan.id] = {"plan": plan, "order_id": oid, "placed": now_ist().isoformat()}
        self._persist_trade({"id": plan.id, "state": TState.ENTRY_WORKING})
        db.log_event("ENTRY_SENT", {"symbol": plan.symbol, "order_id": oid, "trigger": plan.entry_trigger})
        return True

    def on_entry_fill(self, trade_id: str, qty: int, price: float, sector: str = "UNKNOWN") -> Position:
        plan: TradePlan = self.pending[trade_id]["plan"]
        r = max(price - plan.init_stop, 0.01)
        p = Position(id=trade_id, symbol=plan.symbol, strategy=plan.strategy, qty=qty, init_qty=qty,
                     avg_entry=price, init_stop=plan.init_stop, cur_stop=plan.init_stop,
                     t1=price + self.cfg.t1_r_multiple * r, t2=price + self.cfg.t2_r_multiple * r,
                     r=r, entry_date=str(now_ist().date()), high_water=price,
                     state=TState.OPEN, sector=sector)
        self.positions[trade_id] = p
        self.pending.pop(trade_id, None)
        # Protective stop
        resp = self.broker.place_order(symbol=p.symbol, txn=Txn.SELL, qty=qty,
                                       order_type=OrdType.SL_M, trigger_price=p.cur_stop,
                                       ref_id=trade_id.replace("SIG-", "S"))
        self._log_order(trade_id, resp, p.symbol, Txn.SELL, qty, OrdType.SL_M, 0, p.cur_stop)
        self._persist_position(p)
        db.log_event("FILL", {"symbol": p.symbol, "qty": qty, "price": price, "stop": p.cur_stop})

        if self.notifier:
            try:
                self.notifier.notify_trade_filled(p)
            except Exception as e:
                LOG_LC.warning("Telegram notify_trade_filled failed: %s", e)

        return p

    def apply_actions(self, p: Position, acts: List[Action], costs: bool = True) -> float:
        realised = 0.0
        for a in acts:
            if a.kind == "MOVE_STOP":
                p.cur_stop = a.price
                p.be_moved = p.be_moved or a.reason.startswith("BREAKEVEN")
                p.state = TState.TRAILING if a.reason == "TRAIL" else p.state
                db.log_event("MOVE_STOP", {"symbol": p.symbol, "new_stop": a.price, "reason": a.reason})
                if self.notifier:
                    try:
                        self.notifier.notify_stop_moved(p, a.price, a.reason)
                    except Exception as e:
                        LOG_LC.warning("Telegram notify_stop_moved failed: %s", e)
            else:
                qty = min(a.qty, p.qty)
                if qty <= 0:
                    continue
                gross = (a.price - p.avg_entry) * qty
                fee = transaction_costs(a.price, qty, Txn.SELL, self.cfg)["total"] if costs else 0.0
                realised += gross - fee
                p.qty -= qty
                if a.kind == "SCALE_OUT":
                    p.scaled = True
                    p.state = TState.SCALED_OUT
                    if self.notifier:
                        try:
                            self.notifier.notify_target_scaled(p, qty, a.price, a.reason)
                        except Exception as e:
                            LOG_LC.warning("Telegram notify_target_scaled failed: %s", e)
                if p.qty == 0:
                    p.state = TState.CLOSED
                db.log_event("EXIT", {"symbol": p.symbol, "kind": a.kind, "qty": qty, "price": a.price, "reason": a.reason})
        if p.qty == 0:
            exit_reason = acts[-1].reason if acts else ""
            self.positions.pop(p.id, None)
            self._persist_position(p, exit_reason=exit_reason,
                                   closed_at=now_ist().isoformat(),
                                   realized_pnl=round(realised, 2))
            if self.notifier:
                try:
                    self.notifier.notify_trade_closed(p, round(realised, 2), exit_reason)
                except Exception as e:
                    LOG_LC.warning("Telegram notify_trade_closed failed: %s", e)
        else:
            self._persist_position(p)
        return realised


    def manage_all(self, bars: Dict[str, Dict[str, float]], atrs: Dict[str, float],
                   regime_state: str = "NEUTRAL") -> float:
        realised = 0.0
        for p in list(self.positions.values()):
            bar = bars.get(p.symbol)
            if not bar:
                continue
            p.bars_held += 1
            p.high_water = max(p.high_water, bar["high"])
            p.mfe_r = max(p.mfe_r, (bar["high"] - p.avg_entry) / p.r)
            p.mae_r = min(p.mae_r, (bar["low"] - p.avg_entry) / p.r)
            acts = exit_decisions(p, bar, atrs.get(p.symbol, np.nan), self.cfg, regime_state)
            if acts:
                realised += self.apply_actions(p, acts)
        return realised
