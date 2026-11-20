from __future__ import annotations

import math
from typing import Dict

from app.core.config import Config
from app.engine.broker import Txn


def slippage_bps(order_shares: float, adv_shares: float, cfg: Config) -> float:
    if not adv_shares or adv_shares <= 0:
        return cfg.slippage_bps * 3
    participation = min(order_shares / adv_shares, 0.5)
    impact = 45.0 * math.sqrt(max(participation, 0.0))     # sqrt-law market impact
    return cfg.slippage_bps + impact


def transaction_costs(price: float, qty: int, side: str, cfg: Config) -> Dict[str, float]:
    turnover = price * qty
    if turnover <= 0:
        return {"total": 0.0, "bps": 0.0}
    brokerage = min(turnover * cfg.brokerage_pct / 100, cfg.brokerage_cap)
    stt = turnover * 0.001                                  # both legs, delivery
    exch = turnover * 0.0000297
    sebi = turnover * 0.000001
    stamp = turnover * 0.00015 if side == Txn.BUY else 0.0
    dp = 0.0 if side == Txn.BUY else 18.5
    gst = 0.18 * (brokerage + exch + sebi + dp)
    total = brokerage + stt + exch + sebi + stamp + dp + gst
    return {
        "brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi,
        "stamp": stamp, "dp": dp, "gst": gst, "total": total,
        "bps": total / turnover * 10_000
    }
