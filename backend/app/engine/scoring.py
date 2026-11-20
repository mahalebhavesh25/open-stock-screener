from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from app.core.config import Config
from app.engine.strategies import get_active_strategies

LOG_STRAT = logging.getLogger("engine.strategy")
LOG_SCREEN = logging.getLogger("engine.screener")

SCORE_WEIGHTS = {
    "rel_63":        0.20,   # relative strength vs the small-cap index
    "momentum":      0.16,   # blended 63/126-day return
    "trend":         0.15,   # EMA stack + slope
    "proximity":     0.12,   # closeness to the 52-week high
    "volume":        0.12,   # thrust / accumulation
    "tightness":     0.12,   # pre-breakout contraction
    "liquidity":     0.08,   # tradeability
    "consensus":     0.05,   # how many strategies agree
}

STRATEGY_WEIGHT = {
    "VCP_BREAKOUT": 1.00, "TREND_PULLBACK": 1.00, "DONCHIAN_55": 0.95,
    "EPISODIC_PIVOT": 0.95, "MOMENTUM_52W": 0.90, "MEAN_REVERT": 0.80,
}

_SCORE_WARNED = False


def _pct(s: pd.Series) -> pd.Series:
    """Cross-sectional percentile 0-100; NaNs land at median."""
    r = s.rank(pct=True) * 100
    return r.fillna(50.0)


def run_strategies(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Collapse the strategy bank into one signal frame (highest-priority strategy wins a bar)."""
    strategies = get_active_strategies(cfg)
    priority = [
        x for x in ("VCP_BREAKOUT", "TREND_PULLBACK", "DONCHIAN_55",
                    "EPISODIC_PIVOT", "MOMENTUM_52W", "MEAN_REVERT") if x in strategies
    ]
    frames = {k: fn(f, cfg) for k, fn in strategies.items()}
    out = pd.DataFrame(index=f.index, columns=["signal", "entry", "stop", "strategy"])
    out["signal"] = False
    for name in reversed(priority):
        fr = frames[name]
        m = fr["signal"].to_numpy()
        out.loc[m, ["signal", "entry", "stop", "strategy"]] = fr.loc[m, ["signal", "entry", "stop", "strategy"]]
    out["signal"] = out["signal"].fillna(False).astype(bool)
    out["n_confirm"] = sum(fr["signal"].astype(int) for fr in frames.values())
    return out


def strategy_hit_counts(panel: Dict[str, pd.DataFrame], cfg: Config, last_n: int = 1) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    strategies = get_active_strategies(cfg)
    for f in panel.values():
        tail = f.tail(max(last_n + 260, 300))
        for name, fn in strategies.items():
            try:
                counts[name] += int(fn(tail, cfg)["signal"].tail(last_n).any())
            except Exception as e:
                LOG_STRAT.debug("%s failed on a frame: %s", name, e)
    return dict(counts)


def score_candidates(cand: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    global _SCORE_WARNED
    if cand.empty:
        return cand.assign(score=[])
    d = cand.copy()
    comp = pd.DataFrame(index=d.index)
    comp["rel_63"] = _pct(d.get("rel_63", pd.Series(np.nan, index=d.index)))
    comp["momentum"] = 0.5 * _pct(d["ret_63"]) + 0.5 * _pct(d["ret_126"])
    comp["trend"] = 0.6 * _pct(d["ema50_slope"]) + 40 * d["stack"].astype(float)
    comp["proximity"] = _pct(d["dist_52w_high"])
    comp["volume"] = 0.6 * _pct(d["vol_z"]) + 40 * d["pocket_pivot"].astype(float)
    comp["tightness"] = 0.5 * _pct(-d["contraction"]) + 0.5 * _pct(-d["bbw_pct"])
    comp["liquidity"] = _pct(d["turnover_med20_cr"])
    comp["consensus"] = np.clip(d.get("n_confirm", 1).astype(float) * 33, 0, 100)

    raw = sum(comp[k] * w for k, w in SCORE_WEIGHTS.items())

    # Penalties
    pen = pd.Series(0.0, index=d.index)
    pen += np.clip(d["ext_atr"] - 2.5, 0, None) * 4.0            # chasing an extended move
    pen += np.clip(d["atr_pct"] - 6.0, 0, None) * 2.5            # too violent to size properly
    pen += np.clip(d["base_depth"] - 35, 0, None) * 0.25         # sloppy, deep base
    pen += np.where(d["gap_pct"] > 6, 8.0, 0.0)                  # already gapped away
    pen += np.where(~d["above_200"].astype(bool), 10.0, 0.0)     # counter-trend
    pen += np.clip(3.0 - d["turnover_med20_cr"], 0, None) * 3.0  # thin

    weight = d["strategy"].map(STRATEGY_WEIGHT).fillna(0.85)
    d["score"] = np.clip((raw - pen) * weight, 0, 100).round(1)
    for k in comp.columns:
        d[f"c_{k}"] = comp[k].round(1)
    d["penalty"] = pen.round(1)
    if (d["rel_63"].isna().all() if "rel_63" in d else True):
        lvl = logging.DEBUG if _SCORE_WARNED else logging.WARNING
        LOG_SCREEN.log(lvl, "score: rel_63 is NaN for every candidate on %s",
                       str(pd.Timestamp(d["date"].iloc[0]).date()) if "date" in d else "this bar")
        _SCORE_WARNED = True
    return d.sort_values("score", ascending=False)
