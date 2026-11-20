from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from app.core.config import Config
from app.engine.calendar import next_trading_day
from app.engine.regime import Regime

LOG_PLAN = logging.getLogger("engine.plan")
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist() -> datetime:
    return datetime.now(IST)


@dataclass
class SizingResult:
    qty: int
    risk_amount: float
    notional: float
    r_per_share: float
    binding_cap: str
    ok: bool
    reason: str = ""
    stop: float = 0.0


def size_position(entry: float, stop: float, equity: float, cash: float,
                  adv_shares: float, cfg: Config, regime_mult: float = 1.0,
                  open_risk: float = 0.0) -> SizingResult:
    r_per_share = entry - stop
    if r_per_share <= 0 or entry <= 0:
        return SizingResult(0, 0, 0, 0, "invalid", False, "stop is not below entry", stop=0.0)

    stop_dist_pct = r_per_share / entry * 100
    if stop_dist_pct > cfg.max_stop_pct:
        # Instead of rejecting setups due to wide swing lows in volatile small-caps,
        # clamp the initial stop to cfg.max_stop_pct (e.g. 8.0%) to strictly contain risk.
        stop = entry * (1.0 - cfg.max_stop_pct / 100.0)
        r_per_share = entry - stop
        stop_dist_pct = cfg.max_stop_pct

    risk_budget = equity * cfg.risk_per_trade_pct / 100 * regime_mult
    heat_left = equity * cfg.max_portfolio_heat_pct / 100 - open_risk
    if heat_left <= 0:
        return SizingResult(0, 0, 0, r_per_share, "portfolio_heat", False, "book is at max heat", stop=stop)
    risk_budget = min(risk_budget, heat_left)

    qty_risk = risk_budget / r_per_share
    qty_notional = (equity * cfg.max_position_pct / 100) / entry
    qty_adv = (adv_shares or 0) * cfg.max_adv_participation_pct / 100
    qty_cash = cash / entry

    caps = {"risk": qty_risk, "notional": qty_notional, "liquidity": qty_adv, "cash": qty_cash}
    binding = min(caps, key=lambda k: caps[k])
    qty = int(math.floor(min(caps.values())))

    LOG_PLAN.debug("sizing entry=%.2f stop=%.2f (%.1f%%) · caps risk=%.0f notional=%.0f "
                   "liquidity=%.0f cash=%.0f → %d (%s)", entry, stop, stop_dist_pct, qty_risk,
                   qty_notional, qty_adv, qty_cash, max(qty, 0), binding)
    if qty <= 0:
        return SizingResult(0, 0, 0, r_per_share, binding, False, f"{binding} cap allows 0 shares", stop=stop)
    return SizingResult(qty, qty * r_per_share, qty * entry, r_per_share, binding, True, stop=stop)


@dataclass
class TradePlan:
    id: str
    scan_date: str
    symbol: str
    name: str
    sector: str
    strategy: str
    score: float
    entry_type: str            # BUY_STOP | LIMIT
    entry_trigger: float
    valid_till: str
    init_stop: float
    r_per_share: float
    t1: float
    t2: float
    qty: int
    risk_amount: float
    notional: float
    risk_pct_equity: float
    stop_pct: float
    trail_rule: str
    time_stop_bars: int
    regime: str
    binding_cap: str
    features: Dict[str, float] = field(default_factory=dict)
    llm: Dict[str, Any] = field(default_factory=dict)
    thesis: str = ""

    def to_row(self) -> Dict[str, Any]:
        return {"id": self.id, "scan_date": self.scan_date, "symbol": self.symbol,
                "strategy": self.strategy, "score": self.score, "entry_type": self.entry_type,
                "entry_trigger": self.entry_trigger, "stop": self.init_stop, "t1": self.t1,
                "t2": self.t2, "qty": self.qty, "risk_amount": self.risk_amount,
                "r_per_share": self.r_per_share, "regime": self.regime,
                "features": json.dumps(self.features, default=str),
                "llm": json.dumps(self.llm, default=str),
                "status": "PROPOSED", "created_at": now_ist().isoformat()}

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["created_at"] = now_ist().isoformat()
        return d


def build_trade_plan(row: pd.Series, cfg: Config, equity: float, cash: float,
                     regime: Regime, open_risk: float, scan_date: str) -> Optional[TradePlan]:
    entry, orig_stop = float(row["entry"]), float(row["stop"])
    sizing = size_position(entry, orig_stop, equity, cash, float(row.get("adv20", 0) or 0),
                           cfg, regime.exposure_mult, open_risk)
    if not sizing.ok:
        LOG_PLAN.info("✗ %s (%s, score %.1f) not sized: %s", row["symbol"], row.get("strategy"),
                      float(row.get("score", float("nan"))), sizing.reason)
        return None

    stop = sizing.stop if sizing.stop > 0 else (entry - sizing.r_per_share)
    r = sizing.r_per_share
    t1 = entry + cfg.t1_r_multiple * r
    t2 = entry + cfg.t2_r_multiple * r
    if np.isfinite(row.get("base_depth", np.nan)):
        measured = entry * (1 + float(row["base_depth"]) / 100 * 0.9)
        t2 = min(t2, max(measured, t1 * 1.05))

    thesis = (f"{row['strategy'].replace('_',' ').title()} · RS {row.get('rel_63', 0):+.0f}% vs index · "
              f"{row.get('dist_52w_high', 0):+.0f}% from 52w high · ADX {row.get('adx', 0):.0f} · "
              f"ATR {row.get('atr_pct', 0):.1f}%")

    rs = row.get("rel_63", np.nan)
    thesis = thesis.replace(f"RS {row.get('rel_63', 0):+.0f}% vs index",
                            f"RS {rs:+.0f}% vs index" if pd.notna(rs) else "RS n/a (no benchmark)")
    LOG_PLAN.info("✓ %s %s score=%.1f · %s @ %.2f · SL %.2f (%.1f%%) · T1 %.2f · T2 %.2f · qty %d · "
                  "risk ₹%.0f · cap=%s", row["symbol"], row["strategy"], float(row["score"]),
                  "LIMIT" if row["strategy"] == "MEAN_REVERT" else "BUY_STOP", entry, stop,
                  r / entry * 100, t1, t2, sizing.qty, sizing.risk_amount, sizing.binding_cap)
    return TradePlan(
        id=f"SIG-{uuid.uuid4().hex[:10]}", scan_date=scan_date,
        symbol=row["symbol"], name=str(row.get("name", row["symbol"])),
        sector=str(row.get("sector", "UNKNOWN")), strategy=row["strategy"],
        score=float(row["score"]), entry_type="LIMIT" if row["strategy"] == "MEAN_REVERT" else "BUY_STOP",
        entry_trigger=round(entry, 2), valid_till=str(next_trading_day(pd.Timestamp(scan_date).date())),
        init_stop=round(stop, 2), r_per_share=round(r, 2),
        t1=round(t1, 2), t2=round(t2, 2), qty=sizing.qty,
        risk_amount=round(sizing.risk_amount, 0), notional=round(sizing.notional, 0),
        risk_pct_equity=round(sizing.risk_amount / equity * 100, 2),
        stop_pct=round(r / entry * 100, 2),
        trail_rule=f"chandelier {cfg.chandelier_mult}xATR after {cfg.trail_activate_r}R; "
                   f"BE after {cfg.breakeven_after_r}R",
        time_stop_bars=cfg.time_stop_bars, regime=regime.state, binding_cap=sizing.binding_cap,
        features={k: float(row[k]) for k in
                  ("score", "adx", "atr_pct", "rel_63", "ret_63", "dist_52w_high", "vol_ratio",
                   "contraction", "ext_atr", "turnover_med20_cr", "rsi14")
                  if k in row and pd.notna(row[k])},
        thesis=thesis,
    )
