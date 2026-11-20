from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.core.config import Config
from app.engine.data_hub import DataHub
from app.engine.features import compute_features
from app.engine.regime import Regime, assess_regime
from app.engine.scoring import run_strategies, score_candidates

LOG_SCREEN = logging.getLogger("engine.screener")
LOG_FEAT = logging.getLogger("engine.features")
LOG_BENCH = logging.getLogger("engine.benchmark")

FEATURE_COLS = ["close", "open", "high", "low", "atr", "atr_pct", "adx", "rsi14", "rsi2",
                "ema20", "ema50", "ema200", "ema50_slope", "stack", "above_200", "vol_ratio",
                "vol_z", "pocket_pivot", "contraction", "bbw_pct", "dist_52w_high", "base_depth",
                "ext_atr", "gap_pct", "ret_21", "ret_63", "ret_126", "rel_63",
                "turnover_med20_cr", "adv20", "dry_up"]


_PANEL_CACHE: Optional[Tuple[Dict[str, pd.DataFrame], pd.DataFrame]] = None
_PANEL_CACHE_TIME: float = 0.0

def invalidate_panel_cache() -> None:
    global _PANEL_CACHE, _PANEL_CACHE_TIME
    _PANEL_CACHE = None
    _PANEL_CACHE_TIME = 0.0


def compute_panel(cfg: Config, hub: DataHub, universe: pd.DataFrame,
                  refresh: bool = True,
                  checkpoint_callback: Optional[Callable[[int, int, str], bool]] = None
                  ) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
    global _PANEL_CACHE, _PANEL_CACHE_TIME
    if not refresh and _PANEL_CACHE is not None and (time.time() - _PANEL_CACHE_TIME) < 3600:
        LOG_SCREEN.info("compute_panel: returning cached in-memory panel (%d symbols)", len(_PANEL_CACHE[0]))
        return _PANEL_CACHE

    if universe is None or universe.empty:
        raise RuntimeError("compute_panel(): universe is empty")
    syms = universe["symbol"].tolist()
    LOG_SCREEN.info("compute_panel: %d symbols · %d days · refresh=%s", len(syms), cfg.history_days, refresh)

    # Bulk fetch raw bars
    raw = {}
    total_syms = len(syms)
    for i, s in enumerate(syms, 1):
        if checkpoint_callback and not checkpoint_callback(i, total_syms, s):
            LOG_SCREEN.warning("compute_panel interrupted at symbol %d/%d", i, total_syms)
            break
        if i == 1 or i % 50 == 0 or i == total_syms:
            LOG_SCREEN.info("compute_panel: loading historical bars %d/%d symbols (%s)", i, total_syms, s)
        raw[s] = hub.get_daily(s, lookback_days=cfg.history_days, refresh=refresh)

    # Benchmark
    bench_df = hub.benchmark(cfg.history_days, refresh=refresh)

    # Check if benchmark is missing, too short, or fails to cover the panel's historical span
    valid_raw = {s: d for s, d in raw.items() if d is not None and len(d) >= cfg.min_history_bars}
    raw_min_date = min((d.index.min() for d in valid_raw.values()), default=None)

    needs_proxy = (
        bench_df is None or
        len(bench_df) < cfg.min_benchmark_bars or
        (raw_min_date is not None and (bench_df.index.min() - raw_min_date).days > 60)
    )
    if needs_proxy and cfg.benchmark_allow_universe_proxy:
        proxy_df = hub.universe_proxy(raw, cfg.min_history_bars)
        if proxy_df is not None and not proxy_df.empty:
            if bench_df is not None and not bench_df.empty and bench_df.index.min() > proxy_df.index.min():
                stitch_point = bench_df.index.min()
                pre = proxy_df.loc[proxy_df.index < stitch_point]
                if not pre.empty:
                    scale = float(bench_df["close"].iloc[0] / pre["close"].iloc[-1])
                    pre_scaled = pre.copy()
                    for col in ("open", "high", "low", "close"):
                        pre_scaled[col] *= scale
                    bench_df = pd.concat([pre_scaled, bench_df]).sort_index()
                    bench_df = bench_df[~bench_df.index.duplicated(keep="last")]
                    LOG_SCREEN.info("compute_panel: stitched universe proxy (%s -> %s) with benchmark (%s -> %s)",
                                    pre.index.min().date(), pre.index.max().date(),
                                    stitch_point.date(), bench_df.index.max().date())
            else:
                bench_df = proxy_df
                LOG_SCREEN.info("compute_panel: using universe proxy benchmark (%d bars)", len(bench_df))

    if bench_df is None or len(bench_df) < cfg.min_benchmark_bars:
        raise RuntimeError(
            f"benchmark {cfg.benchmark!r} returned {0 if bench_df is None else len(bench_df)} bars (need ≥{cfg.min_benchmark_bars})")
    bench_close = bench_df["close"]

    panel: Dict[str, pd.DataFrame] = {}
    for i, s in enumerate(syms, 1):
        df = raw.get(s)
        if df is None or len(df) < cfg.min_history_bars:
            continue
        try:
            panel[s] = compute_features(df, bench_close)
        except Exception as e:
            LOG_FEAT.warning("features failed for %s: %s", s, e)
        if i == 1 or i % 50 == 0 or i == total_syms:
            LOG_SCREEN.info("compute_panel: calculated features %d/%d symbols (%s)", i, total_syms, s)
    bench_feat = compute_features(bench_df)
    LOG_SCREEN.info("compute_panel: completed panel generation for %d/%d valid symbols", len(panel), total_syms)
    _PANEL_CACHE = (panel, bench_feat)
    _PANEL_CACHE_TIME = time.time()
    return panel, bench_feat


def breadth_pct(panel: Dict[str, pd.DataFrame], asof: Optional[pd.Timestamp] = None) -> float:
    vals = []
    for f in panel.values():
        row = f.iloc[-1] if asof is None else (f.loc[:asof].iloc[-1] if len(f.loc[:asof]) else None)
        if row is not None and np.isfinite(row.get("ema50", np.nan)):
            vals.append(float(row["close"] > row["ema50"]))
    if not vals:
        return 50.0
    return 100 * float(np.mean(vals))


def collect_candidates(panel: Dict[str, pd.DataFrame], universe: pd.DataFrame, cfg: Config,
                       asof: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    meta = universe.set_index("symbol")[["name", "sector"]].to_dict("index") if not universe.empty else {}
    rows = []
    for sym, f in panel.items():
        ff = f if asof is None else f.loc[:asof]
        if len(ff) < cfg.min_history_bars:
            continue
        sig = run_strategies(ff, cfg)
        last = sig.iloc[-1]
        if not bool(last["signal"]):
            continue
        feat = ff.iloc[-1]
        row = {"symbol": sym, "date": ff.index[-1],
               "strategy": last["strategy"], "entry": float(last["entry"]),
               "stop": float(last["stop"]), "n_confirm": int(last["n_confirm"])}
        row.update({c: float(feat[c]) if not isinstance(feat[c], (bool, np.bool_)) else bool(feat[c])
                    for c in FEATURE_COLS if c in feat})
        row.update(meta.get(sym, {"name": sym, "sector": "UNKNOWN"}))
        rows.append(row)
    return pd.DataFrame(rows)


def apply_hard_filters(cand: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    if cand.empty:
        return cand
    gates = {
        "turnover": cand["turnover_med20_cr"].ge(cfg.min_median_turnover_cr),
        "atr_band": cand["atr_pct"].between(cfg.min_atr_pct, cfg.max_atr_pct),
        "price_band": cand["close"].between(cfg.price_min, cfg.price_max),
        "stop<entry": cand["stop"] < cand["entry"],
        "stop≤15%": ((cand["entry"] - cand["stop"]) / cand["entry"] * 100).le(15),
    }
    m = pd.Series(True, index=cand.index)
    for g in gates.values():
        m &= g
    return cand.loc[m].copy()


def run_screener(cfg: Config, hub: DataHub, universe: pd.DataFrame,
                 refresh: bool = True, panel: Optional[Dict[str, pd.DataFrame]] = None,
                 bench_feat: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    t0 = time.time()
    if panel is None:
        panel, bench_feat = compute_panel(cfg, hub, universe, refresh)
    if bench_feat is None:
        raise RuntimeError("run_screener(): got a panel but no bench_feat")
    br = breadth_pct(panel)
    regime = assess_regime(bench_feat, br, cfg)

    cand = apply_hard_filters(collect_candidates(panel, universe, cfg), cfg)
    scored = score_candidates(cand, cfg) if not cand.empty else cand
    if not scored.empty:
        qualified = scored[scored["score"] >= cfg.min_score]
        if not qualified.empty:
            scored = qualified
        else:
            fallback = scored[scored["score"] >= 50.0]
            scored = fallback if not fallback.empty else scored.head(5)
    LOG_SCREEN.info("scan: %d signals after hard filters → %d after score | regime=%s | %.1fs",
                    len(cand), len(scored), regime.state, time.time() - t0)
    return {
        "candidates": scored,
        "regime": regime,
        "panel": panel,
        "bench": bench_feat,
        "breadth": br
    }
