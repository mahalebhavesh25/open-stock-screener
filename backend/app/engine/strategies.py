from __future__ import annotations

import logging
from typing import Callable, Dict

import numpy as np
import pandas as pd

from app.core.config import CFG, Config

LOG_STRAT = logging.getLogger("engine.strategy")


def _pack(sig: pd.Series, entry: pd.Series, stop: pd.Series, name: str) -> pd.DataFrame:
    out = pd.DataFrame({"signal": sig.fillna(False).astype(bool),
                        "entry": entry, "stop": stop})
    out["strategy"] = name
    bad = (~np.isfinite(out["entry"])) | (~np.isfinite(out["stop"])) | (out["stop"] >= out["entry"])
    out.loc[bad, "signal"] = False
    return out


def strat_vcp_breakout(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c, h, l = f["close"], f["high"], f["low"]
    sig = (
        f["stack"]
        & (f["ema50_slope"] > 0)
        & (c > f["hh20"])
        & (c.shift(1) <= f["hh20"])
        & (f["vol_ratio"] > 1.5)
        & (f["contraction"] < 0.95)
        & (f["dist_52w_high"] > -25)
        & (f["adx"] > cfg.min_adx)
        & f["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct)
        & (f["ext_atr"] < cfg.max_extension_atr)
        & (f["base_depth"] < 45)
    )
    entry = np.maximum(h, f["hh20"]) * 1.002
    stop = np.minimum(c - cfg.atr_stop_mult * f["atr"], f["ll10"] * 0.995)
    return _pack(sig, entry, stop, "VCP_BREAKOUT")


def strat_trend_pullback(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c, h, l = f["close"], f["high"], f["low"]
    touched = (l <= f["ema20"] * 1.01).rolling(4).max().astype(bool)
    reversal = (c > c.shift(1)) & (c > (h + l) / 2) & (l > l.shift(1) * 0.995)
    sig = (
        f["stack"] & (f["ema50_slope"] > 0)
        & touched & reversal
        & (c > f["ema50"])
        & f["rsi14"].shift(1).between(35, 58)
        & (f["dry_up"] < 1.15)
        & (f["adx"] > cfg.min_adx - 3)
        & f["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct)
        & (f["dist_52w_high"] > -30)
    )
    entry = h * 1.002
    stop = np.minimum(l * 0.99, c - cfg.atr_stop_mult * 0.9 * f["atr"])
    return _pack(sig, entry, stop, "TREND_PULLBACK")


def strat_mean_revert(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c, l = f["close"], f["low"]
    sig = (
        f["above_200"] & (f["ema200_slope"] > 0)
        & (f["rsi2"] < 8)
        & (c < f["ema20"]) & (c > f["ema200"] * 1.02)
        & (f["ret_5"] < -4)
        & f["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct)
        & (f["turnover_med20_cr"] > cfg.min_median_turnover_cr)
    )
    entry = c * 1.003
    stop = np.minimum(l, c - 2.0 * f["atr"]) * 0.995
    return _pack(sig, entry, stop, "MEAN_REVERT")


def strat_donchian(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c, h = f["close"], f["high"]
    sig = (
        (c > f["hh55"]) & (c.shift(1) <= f["hh55"])
        & (f["adx"] > 25) & (f["pdi"] > f["mdi"])
        & f["above_200"] & (f["ema200_slope"] > 0)
        & f["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct)
        & (f["ext_atr"] < cfg.max_extension_atr + 1)
    )
    entry = np.maximum(h, f["hh55"]) * 1.002
    stop = c - cfg.atr_stop_mult * 1.15 * f["atr"]
    return _pack(sig, entry, stop, "DONCHIAN_55")


def strat_52w_momentum(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c, h, l = f["close"], f["high"], f["low"]
    sig = (
        f["above_200"] & (f["ema200_slope"] > 0)
        & (f["dist_52w_high"] > -4.0)
        & (f["ret_126"] > 12)
        & (f["vol_ratio"] > 1.15)
        & (f["adx"] > 20)
        & f["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct)
    )
    entry = h * 1.002
    stop = np.maximum(c - 2.2 * f["atr"], f["ema20"] - 0.5 * f["atr"])
    return _pack(sig, entry, stop, "MOMENTUM_52W")


def strat_episodic_pivot(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c, h, l, o = f["close"], f["high"], f["low"], f["open"]
    gap = (o / f["close"].shift(1) - 1) * 100
    sig = (
        f["above_200"] & (f["ema200_slope"] > 0)
        & gap.between(3.0, 12.0)
        & (f["vol_ratio"] > 2.5)
        & (c > o)
        & (c > f["hh20"])
        & (f["ext_atr"] < cfg.max_extension_atr + 1.0)
        & f["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct)
    )
    entry = h * 1.002
    stop = np.maximum(l * 0.995, c - 2.4 * f["atr"])
    return _pack(sig, entry, stop, "EPISODIC_PIVOT")


def get_active_strategies(cfg: Config) -> Dict[str, Callable[[pd.DataFrame, Config], pd.DataFrame]]:
    strats = {
        "VCP_BREAKOUT": strat_vcp_breakout,
        "TREND_PULLBACK": strat_trend_pullback,
        "DONCHIAN_55": strat_donchian,
    }
    if cfg.enable_mean_revert:
        strats["MEAN_REVERT"] = strat_mean_revert
    if cfg.enable_52w_momentum:
        strats["MOMENTUM_52W"] = strat_52w_momentum
    if cfg.enable_episodic_pivot:
        strats["EPISODIC_PIVOT"] = strat_episodic_pivot
    return strats
