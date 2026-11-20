from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np
import pandas as pd

LOG_FEAT = logging.getLogger("engine.features")

FEATURE_COLS = ["close", "open", "high", "low", "atr", "atr_pct", "adx", "rsi14", "rsi2",
                "ema20", "ema50", "ema200", "ema50_slope", "stack", "above_200", "vol_ratio",
                "vol_z", "pocket_pivot", "contraction", "bbw_pct", "dist_52w_high", "base_depth",
                "ext_atr", "gap_pct", "ret_21", "ret_63", "ret_126", "rel_63",
                "turnover_med20_cr", "adv20", "dry_up"]


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def true_range(h: pd.Series, l: pd.Series, c: pd.Series) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = _wilder(d.clip(lower=0), n)
    dn = _wilder((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def adx(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    atr_ = _wilder(true_range(h, l, c), n).replace(0, np.nan)
    pdi = 100 * _wilder(plus, n) / atr_
    mdi = 100 * _wilder(minus, n) / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _wilder(dx, n), pdi, mdi


def compute_features(df: pd.DataFrame, bench: Optional[pd.Series] = None) -> pd.DataFrame:
    """One symbol in, ~45 feature columns out. Index must be a DatetimeIndex of daily bars."""
    if df is None or len(df) == 0:
        raise ValueError("compute_features() got an empty frame — nothing to compute")
    missing = [c for c in ("open", "high", "low", "close", "volume") if c not in df.columns]
    if missing:
        raise ValueError(f"compute_features() input is missing columns {missing}")
    f = df[["open", "high", "low", "close", "volume"]].copy().astype(float)
    o, h, l, c, v = f["open"], f["high"], f["low"], f["close"], f["volume"]

    # --- trend ---
    for n in (10, 20, 50, 200):
        f[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    f["sma20"] = c.rolling(20).mean()
    f["ema50_slope"] = f["ema50"].pct_change(10) * 100
    f["ema200_slope"] = f["ema200"].pct_change(20) * 100
    f["above_20"] = c > f["ema20"]
    f["above_50"] = c > f["ema50"]
    f["above_200"] = c > f["ema200"]
    f["stack"] = (f["ema20"] > f["ema50"]) & (f["ema50"] > f["ema200"])

    # --- momentum ---
    for n in (5, 21, 63, 126, 252):
        f[f"ret_{n}"] = c.pct_change(n) * 100
    f["rsi14"] = rsi(c, 14)
    f["rsi2"] = rsi(c, 2)
    f["adx"], f["pdi"], f["mdi"] = adx(h, l, c, 14)

    # --- volatility ---
    f["atr"] = _wilder(true_range(h, l, c), 14)
    f["atr_pct"] = f["atr"] / c * 100
    f["atr_pct_50"] = f["atr_pct"].rolling(50).mean()
    f["contraction"] = f["atr_pct"].rolling(10).mean() / f["atr_pct_50"]
    sd = c.rolling(20).std()
    f["bbw"] = (4 * sd) / f["sma20"] * 100
    f["bbw_pct"] = f["bbw"].rolling(120).rank(pct=True) * 100
    rng = h - l
    f["nr7"] = rng == rng.rolling(7).min()
    f["inside"] = (h < h.shift(1)) & (l > l.shift(1))

    # --- volume ---
    f["vol_sma50"] = v.rolling(50).mean()
    f["vol_ratio"] = v / f["vol_sma50"]
    f["vol_z"] = (v - f["vol_sma50"]) / v.rolling(50).std()
    f["dry_up"] = v.rolling(5).mean() / f["vol_sma50"]
    up_vol = v.where(c > c.shift(1), 0.0)
    dn_vol = v.where(c < c.shift(1), np.nan)
    f["pocket_pivot"] = up_vol > dn_vol.rolling(10).max()
    f["turnover_cr"] = c * v / 1e7
    f["adv20"] = v.rolling(20).median()
    f["turnover_med20_cr"] = f["turnover_cr"].rolling(20).median()

    # --- structure ---
    f["hh20"] = h.rolling(20).max().shift(1)
    f["hh55"] = h.rolling(55).max().shift(1)
    f["ll10"] = l.rolling(10).min().shift(1)
    f["ll20"] = l.rolling(20).min().shift(1)
    f["high_252"] = h.rolling(252, min_periods=60).max()
    f["low_252"] = l.rolling(252, min_periods=60).min()
    f["dist_52w_high"] = (c / f["high_252"] - 1) * 100
    f["off_52w_low"] = (c / f["low_252"] - 1) * 100
    f["base_len"] = (~(h >= f["high_252"].shift(1))).astype(int).groupby(
        (h >= f["high_252"].shift(1)).cumsum()).cumsum()
    f["base_depth"] = (h.rolling(40).max() - l.rolling(40).min()) / h.rolling(40).max() * 100
    f["ext_atr"] = (c - f["ema20"]) / f["atr"]
    f["gap_pct"] = (o / c.shift(1) - 1) * 100

    # --- relative strength ---
    if bench is not None and len(bench) > 10:
        b = bench.reindex(f.index).ffill()
        f["rs_line"] = c / b
        f["rs_slope"] = f["rs_line"].pct_change(21) * 100
        f["rs_new_high"] = f["rs_line"] >= f["rs_line"].rolling(63).max()
        f["rel_63"] = f["ret_63"] - (b.pct_change(63) * 100)
        f["bench_ret_21"] = b.pct_change(21) * 100
    else:
        for col in ("rs_line", "rs_slope", "rel_63", "bench_ret_21"):
            f[col] = np.nan
        f["rs_new_high"] = False

    f["quality"] = (f["ret_63"].rolling(63).mean() /
                    (f["ret_5"].rolling(63).std().replace(0, np.nan)))

    # Daily-price sanity flags
    prev = f["close"].shift(1)
    ret1 = (f["close"] / prev - 1) * 100
    flat_bar = ((f["open"] - f["high"]).abs() < 1e-9) & ((f["high"] - f["low"]).abs() < 1e-9) & ((f["low"] - f["close"]).abs() < 1e-9)
    near_band = pd.concat([(ret1.abs() - x).abs() for x in (2, 5, 10, 20)], axis=1).min(axis=1) < 0.35
    f["circuit_like"] = flat_bar & near_band
    f["lower_circuit_locked"] = f["circuit_like"] & (ret1 < 0)
    f["corp_action_suspect"] = (ret1.abs() > 35) & (f["volume"] > 0)
    return f
