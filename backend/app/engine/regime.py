from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict

import numpy as np
import pandas as pd

from app.core.config import Config

LOG_REGIME = logging.getLogger("engine.regime")


@dataclass
class Regime:
    state: str
    breadth: float
    bench_above_50: bool
    bench_above_200: bool
    bench_dd: float
    exposure_mult: float
    max_new: int
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def assess_regime(bench_feat: pd.DataFrame, breadth_pct: float, cfg: Config,
                  verbose: bool = True) -> Regime:
    if bench_feat is None or len(bench_feat) == 0:
        raise RuntimeError(f"assess_regime(): benchmark {cfg.benchmark!r} is empty")
    need = [c for c in ("close", "ema50", "ema200") if c not in bench_feat.columns]
    if need:
        raise RuntimeError(f"assess_regime(): benchmark lacks {need}")
    valid = bench_feat.dropna(subset=["close", "ema50", "ema200"])
    if valid.empty:
        raise RuntimeError("assess_regime(): benchmark has no mature row")
    last = valid.iloc[-1]
    close = valid["close"].astype(float)
    a50 = bool(last["close"] > last["ema50"])
    a200 = bool(last["close"] > last["ema200"])
    dd = float(last["close"] / close.cummax().iloc[-1] - 1) * 100
    vol20 = float(close.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252) * 100)
    if not np.isfinite(vol20):
        vol20 = 25.0

    breadth_component = np.clip((breadth_pct - 20.0) / 60.0, 0, 1)
    trend_component = 0.55 * float(a200) + 0.45 * float(a50)
    dd_component = np.clip((dd + 20.0) / 20.0, 0, 1)
    vol_component = np.clip((45.0 - vol20) / 30.0, 0, 1)
    raw = 0.36 * breadth_component + 0.30 * trend_component + 0.22 * dd_component + 0.12 * vol_component
    mult = float(np.clip(cfg.min_exposure_mult + raw * (cfg.max_exposure_mult - cfg.min_exposure_mult),
                         cfg.min_exposure_mult, cfg.max_exposure_mult))

    if mult >= 0.72 and breadth_pct >= cfg.regime_risk_on_breadth:
        state = "RISK_ON"
    elif mult <= 0.38 or breadth_pct <= cfg.regime_risk_off_breadth or (not a200 and dd < -12):
        state = "RISK_OFF"
    else:
        state = "NEUTRAL"
    mx = 0 if state == "RISK_OFF" else max(1, int(round(cfg.max_new_per_day * mult)))
    note = (f"breadth {breadth_pct:.0f}% · index {'>' if a50 else '<'}50EMA "
            f"{'>' if a200 else '<'}200EMA · DD {dd:.1f}% · vol20 {vol20:.1f}% · exposure {mult:.2f}")
    if verbose:
        LOG_REGIME.info("→ %s · continuous exposure ×%.2f · max %d new/day | %s", state, mult, mx, note)
    return Regime(state, round(breadth_pct, 1), a50, a200, round(dd, 2), round(mult, 3), mx, note)
