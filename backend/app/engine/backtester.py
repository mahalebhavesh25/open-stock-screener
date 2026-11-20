from __future__ import annotations

import logging
import math
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from app.core.config import Config
from app.engine.broker import Txn
from app.engine.features import FEATURE_COLS
from app.engine.frictions import slippage_bps, transaction_costs
from app.engine.lifecycle import Position, exit_decisions
from app.engine.regime import Regime, assess_regime
from app.engine.scoring import run_strategies, score_candidates
from app.engine.trade_planner import size_position

LOG_BT = logging.getLogger("engine.backtest")

BLEND_STRATEGY_MAP: Dict[str, set[str]] = {
    "BLEND_MOMENTUM_BREAKOUT": {"TREND_PULLBACK", "VCP_BREAKOUT", "MOMENTUM_52W"},
    "BLEND_PULLBACK_BREAKOUT": {"TREND_PULLBACK", "VCP_BREAKOUT", "MOMENTUM_52W"},
    "BLEND": {"TREND_PULLBACK", "VCP_BREAKOUT", "MOMENTUM_52W"},
}


@dataclass
class BTFill:
    date: str
    symbol: str
    side: str
    qty: int
    price: float
    fees: float
    reason: str


class Backtester:
    def __init__(self, cfg: Config, panel: Dict[str, pd.DataFrame], universe: pd.DataFrame,
                 bench_feat: pd.DataFrame, strategy: Optional[str] = None):
        if not panel:
            raise ValueError("Backtester: panel is empty")
        self.cfg, self.panel = cfg, panel
        self.strategy = strategy
        self.bench = bench_feat if bench_feat is not None else pd.DataFrame()
        self.meta = universe.set_index("symbol")[["name", "sector"]].to_dict("index") if not universe.empty else {}

        # 1. Precompute breadth across all dates in one vectorized operation
        closes_above = pd.concat([
            (f["close"] > f["ema50"]).astype(float)
            for f in panel.values()
            if "close" in f.columns and "ema50" in f.columns
        ], axis=1)
        self.breadth_by_date: Dict[pd.Timestamp, float] = (closes_above.mean(axis=1) * 100.0).dropna().to_dict()

        # 2. Extract signals and pre-score candidates vectorially
        cand_dfs = []
        for s, f in panel.items():
            sig = run_strategies(f, cfg)
            sig_active = sig[sig["signal"] == True]
            if sig_active.empty:
                continue
            cols_avail = [c for c in FEATURE_COLS if c in f.columns]
            merged = sig_active[["strategy", "entry", "stop", "n_confirm"]].join(f[cols_avail], how="inner")
            if "atr" in merged.columns and "ema200" in merged.columns:
                merged = merged[np.isfinite(merged["atr"]) & np.isfinite(merged["ema200"])]
            if merged.empty:
                continue
            merged["symbol"] = s
            meta_info = self.meta.get(s, {"name": s, "sector": "UNKNOWN"})
            merged["name"] = meta_info.get("name", s)
            merged["sector"] = meta_info.get("sector", "UNKNOWN")
            merged["date"] = merged.index
            cand_dfs.append(merged)

        from app.engine.screener import apply_hard_filters
        if cand_dfs:
            all_cands = pd.concat(cand_dfs, ignore_index=True)
            all_cands = apply_hard_filters(all_cands, cfg)
            if not all_cands.empty:
                all_scored = score_candidates(all_cands, cfg)
                all_scored = all_scored[all_scored["score"] >= cfg.min_score]
                if self.strategy and self.strategy.upper() != "ALL":
                    strat_upper = self.strategy.upper()
                    if strat_upper in BLEND_STRATEGY_MAP:
                        allowed = BLEND_STRATEGY_MAP[strat_upper]
                        all_scored = all_scored[all_scored["strategy"].isin(allowed)]
                    else:
                        all_scored = all_scored[all_scored["strategy"] == strat_upper]
                self.scored_by_date = {d: group for d, group in all_scored.groupby("date")}
                n_scored = len(all_scored)
            else:
                self.scored_by_date = {}
                n_scored = 0
        else:
            self.scored_by_date = {}
            n_scored = 0

        LOG_BT.info("Backtester initialized with %d symbols | Pre-scored %d candidates across %d trading dates (Strategy focus: %s)",
                    len(panel), n_scored, len(self.scored_by_date), self.strategy or "ALL")

    def _breadth(self, d: pd.Timestamp) -> float:
        return self.breadth_by_date.get(d, 50.0)

    def run(self, start: str, end: str, verbose: bool = True) -> Dict[str, Any]:
        cfg = self.cfg
        dates = sorted(set().union(*[set(f.index) for f in self.panel.values()]))
        dates = [d for d in dates if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
        if not dates:
            raise ValueError(f"no bars in requested window {start} → {end}")

        LOG_BT.info("Starting Backtest simulation [%s -> %s] (%d trading days) | Capital: ₹%s | Strategy: %s",
                    start, end, len(dates), f"{cfg.equity:,.0f}", self.strategy or "ALL")

        cash = cfg.equity
        book: Dict[str, Position] = {}
        pending: List[Dict[str, Any]] = []
        fills: List[BTFill] = []
        trades: List[Dict[str, Any]] = []
        entry_fee_per_share: Dict[str, float] = {}
        curve: List[Dict[str, Any]] = []
        peak_equity = cfg.equity
        halted = False

        for i_day, d in enumerate(dates, 1):
            needed_syms = {p.symbol for p in book.values()} | {pe["symbol"] for pe in pending}
            bars = {s: self.panel[s].loc[d] for s in needed_syms if d in self.panel[s].index}

            # 1. Manage open positions
            breadth = self._breadth(d)
            bf = self.bench.loc[:d] if not self.bench.empty else self.bench
            regime = assess_regime(bf, breadth, cfg, verbose=False) if len(bf) > 200 else \
                Regime("NEUTRAL", breadth, True, True, 0.0, 0.65, cfg.max_new_per_day)

            for p in list(book.values()):
                b = bars.get(p.symbol)
                if b is None:
                    continue
                bar = {k: float(b[k]) for k in ("open", "high", "low", "close")}
                bar["lower_circuit_locked"] = bool(b.get("lower_circuit_locked", False))
                p.bars_held += 1
                p.high_water = max(p.high_water, bar["high"])
                p.mfe_r = max(p.mfe_r, (bar["high"] - p.avg_entry) / p.r)
                p.mae_r = min(p.mae_r, (bar["low"] - p.avg_entry) / p.r)
                for a in exit_decisions(p, bar, float(b["atr"]), cfg, regime.state):
                    if a.kind == "MOVE_STOP":
                        p.cur_stop = a.price
                        p.be_moved = p.be_moved or a.reason.startswith("BREAKEVEN")
                        continue
                    qty = min(a.qty, p.qty)
                    if qty <= 0:
                        continue
                    px = a.price * (1 - slippage_bps(qty, float(b["adv20"]), cfg) / 10_000)
                    fee = transaction_costs(px, qty, Txn.SELL, cfg)["total"]
                    cash += px * qty - fee
                    p.qty -= qty
                    if a.kind == "SCALE_OUT":
                        p.scaled = True
                    fills.append(BTFill(str(d.date()), p.symbol, "SELL", qty, px, fee, a.reason))
                    trades.append({
                        "exit_date": str(d.date()), "symbol": p.symbol,
                        "strategy": p.strategy, "entry_date": p.entry_date,
                        "entry": p.avg_entry, "exit": px, "qty": qty,
                        "pnl": (px - p.avg_entry) * qty - fee - entry_fee_per_share.get(p.id, 0.0) * qty,
                        "r": ((px - p.avg_entry) * qty - fee - entry_fee_per_share.get(p.id, 0.0) * qty) / max(p.r * qty, 1e-9),
                        "reason": a.reason, "bars": p.bars_held,
                        "mfe_r": p.mfe_r, "mae_r": p.mae_r,
                        "sector": p.sector, "fees": fee + entry_fee_per_share.get(p.id, 0.0) * qty
                    })
                    if p.qty == 0:
                        book.pop(p.id, None)

            # 2. Work yesterday's pending entries
            for pe in pending:
                b = bars.get(pe["symbol"])
                if b is None:
                    continue
                o, h, l = float(b["open"]), float(b["high"]), float(b["low"])
                trg = pe["trigger"]
                px = None
                if pe["type"] == "BUY_STOP":
                    if o > trg * (1 + cfg.max_entry_gap_pct / 100):
                        pass
                    elif h >= trg:
                        px = max(trg, o)
                else:
                    if l <= trg:
                        px = min(trg, o)
                if px is None:
                    continue
                px *= (1 + slippage_bps(pe["qty"], float(b["adv20"]), cfg) / 10_000)
                qty = pe["qty"]
                fee = transaction_costs(px, qty, Txn.BUY, cfg)["total"]
                if px * qty + fee > cash:
                    qty = int((cash - fee) // px)
                if qty <= 0 or len(book) >= cfg.max_positions:
                    continue
                fee = transaction_costs(px, qty, Txn.BUY, cfg)["total"]
                cash -= px * qty + fee
                r = max(px - pe["stop"], 0.01)
                tid = f"BT-{uuid.uuid4().hex[:8]}"
                book[tid] = Position(id=tid, symbol=pe["symbol"], strategy=pe["strategy"],
                                     qty=qty, init_qty=qty, avg_entry=px, init_stop=pe["stop"],
                                     cur_stop=pe["stop"], t1=px + cfg.t1_r_multiple * r,
                                     t2=px + cfg.t2_r_multiple * r, r=r,
                                     entry_date=str(d.date()), high_water=px,
                                     sector=pe["sector"])
                entry_fee_per_share[tid] = fee / max(qty, 1)
                fills.append(BTFill(str(d.date()), pe["symbol"], "BUY", qty, px, fee, "ENTRY"))
            pending = []

            # 3. Mark to market
            pos_val = sum(p.qty * float(bars[p.symbol]["close"])
                          for p in book.values() if p.symbol in bars)
            equity = cash + pos_val
            peak_equity = max(peak_equity, equity)
            dd = equity / peak_equity - 1
            halted = dd * 100 <= -cfg.max_drawdown_halt_pct
            open_risk = sum(max(p.avg_entry - p.cur_stop, 0) * p.qty for p in book.values())
            curve.append({"date": str(d.date()), "equity": round(equity, 2), "cash": round(cash, 2),
                          "positions": len(book), "open_risk_pct": round(open_risk / max(equity, 1) * 100, 2),
                          "regime": regime.state, "breadth": round(breadth, 1), "dd": round(dd * 100, 2)})

            if i_day == 1 or i_day % 50 == 0 or i_day == len(dates):
                LOG_BT.info("Backtest progress: Day %d/%d (%s) | Equity: ₹%s | Cash: ₹%s | Open pos: %d | Regime: %s",
                            i_day, len(dates), d.date(), f"{equity:,.0f}", f"{cash:,.0f}", len(book), regime.state)

            # 4. Scan for tomorrow
            slots = cfg.max_positions - len(book)
            if halted or slots <= 0 or regime.max_new <= 0:
                continue
            scored = self.scored_by_date.get(d)
            if scored is None or scored.empty:
                continue
            held = {p.symbol for p in book.values()}
            sec_count = defaultdict(int)
            for p in book.values():
                sec_count[p.sector] += 1

            taken = 0
            for _, row in scored.iterrows():
                if taken >= min(regime.max_new, slots):
                    break
                if row["symbol"] in held or any(x["symbol"] == row["symbol"] for x in pending):
                    continue
                if sec_count[row.get("sector", "UNKNOWN")] >= cfg.max_sector_positions:
                    continue
                sz = size_position(float(row["entry"]), float(row["stop"]), equity, cash,
                                   float(row.get("adv20", 0)), cfg, regime.exposure_mult, open_risk)
                if not sz.ok:
                    continue
                stop_px = float(row["entry"]) - sz.r_per_share
                pending.append({"symbol": row["symbol"], "trigger": float(row["entry"]),
                                "stop": stop_px, "qty": sz.qty,
                                "strategy": row["strategy"], "sector": row.get("sector", "UNKNOWN"),
                                "type": "LIMIT" if row["strategy"] == "MEAN_REVERT" else "BUY_STOP",
                                "score": float(row["score"])})
                open_risk += sz.risk_amount
                sec_count[row.get("sector", "UNKNOWN")] += 1
                taken += 1

        curve_df = pd.DataFrame(curve)
        trades_df = pd.DataFrame(trades)
        metrics = performance_metrics(curve_df.set_index("date"), trades_df, self.cfg)

        LOG_BT.info(
            "Backtest complete [%s -> %s]: %d days simulated | %d trades executed | Final Equity: ₹%s | CAGR: %.1f%% | Sharpe: %.2f | Win Rate: %.1f%% | MaxDD: %.1f%%",
            start, end, len(dates), len(trades), f"{equity:,.0f}",
            metrics.get("cagr_pct", 0.0), metrics.get("sharpe_ratio", 0.0),
            metrics.get("win_rate_pct", 0.0), metrics.get("max_drawdown_pct", 0.0)
        )

        return {
            "curve": curve_df.to_dict(orient="records"),
            "trades": trades_df.to_dict(orient="records") if not trades_df.empty else [],
            "fills": [asdict(f) for f in fills],
            "metrics": metrics
        }


def performance_metrics(curve: pd.DataFrame, trades: pd.DataFrame, cfg: Config) -> Dict[str, Any]:
    if curve.empty:
        return {}
    eq = curve["equity"].astype(float)
    rets = eq.pct_change().dropna()
    years = max(len(eq) / 252.0, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if eq.iloc[0] > 0 else 0
    vol = rets.std() * math.sqrt(252)
    downside = rets[rets < 0].std() * math.sqrt(252)
    dd = eq / eq.cummax() - 1
    m: Dict[str, Any] = {
        "start": str(curve.index[0]), "end": str(curve.index[-1]),
        "years": round(years, 2), "final_equity": round(float(eq.iloc[-1]), 0),
        "total_return_pct": float(round(float(eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2)),
        "cagr_pct": float(round(float(cagr) * 100, 2)),
        "vol_pct": float(round(float(vol) * 100, 2)) if np.isfinite(vol) else 0.0,
        "sharpe": float(round(float(rets.mean() / rets.std() * math.sqrt(252)), 2)) if len(rets) and rets.std() else 0.0,
        "sortino": float(round(float(rets.mean() * 252 / downside), 2)) if downside and downside > 0 else 0.0,
        "max_dd_pct": float(round(float(dd.min()) * 100, 2)),
        "calmar": float(round(float(cagr / abs(dd.min())), 2)) if dd.min() < 0 else 0.0,
        "avg_positions": float(round(float(curve["positions"].mean()), 2)),
        "time_in_market_pct": float(round(float((curve["positions"] > 0).mean()) * 100, 1)),
        "pct_days_risk_off": float(round(float((curve["regime"] == "RISK_OFF").mean()) * 100, 1)),
        # UI Aliases
        "sharpe_ratio": float(round(float(rets.mean() / rets.std() * math.sqrt(252)), 2)) if len(rets) and rets.std() else 0.0,
        "sortino_ratio": float(round(float(rets.mean() * 252 / downside), 2)) if downside and downside > 0 else 0.0,
        "max_drawdown_pct": float(round(float(dd.min()) * 100, 2)),
        "win_rate_pct": 0.0,
        "total_trades": 0,
    }
    if trades is not None and not trades.empty:
        t = trades.copy()
        w, l = t[t["pnl"] > 0], t[t["pnl"] <= 0]
        capital_days = float((t["bars"].clip(lower=1) * (t["entry"] * t["qty"])).sum())
        net_pnl = float(t["pnl"].sum())
        hit_rate = float(round(len(w) / len(t) * 100, 1))
        m.update({
            "n_exits": int(len(t)),
            "total_trades": int(len(t)),
            "hit_rate_pct": hit_rate,
            "win_rate_pct": hit_rate,
            "avg_win_r": float(round(float(w["r"].mean()), 2)) if len(w) else 0.0,
            "avg_loss_r": float(round(float(l["r"].mean()), 2)) if len(l) else 0.0,
            "expectancy_r": float(round(float(t["r"].mean()), 3)),
            "profit_factor": float(round(float(w["pnl"].sum() / abs(l["pnl"].sum())), 2))
                             if len(l) and l["pnl"].sum() != 0 else float("inf"),
            "avg_bars_held": float(round(float(t["bars"].mean()), 1)),
            "median_bars_held": float(round(float(t["bars"].median()), 1)),
            "return_per_capital_day_bps": float(round(net_pnl / max(capital_days, 1) * 10_000, 3)),
            "total_fees": float(round(float(t["fees"].sum()), 0)),
            "best_r": float(round(float(t["r"].max()), 2)),
            "worst_r": float(round(float(t["r"].min()), 2)),
        })
        strat_breakdown: Dict[str, Any] = {}
        if "strategy" in t.columns:
            for s_name, s_trades in t.groupby("strategy"):
                s_w = s_trades[s_trades["pnl"] > 0]
                s_l = s_trades[s_trades["pnl"] <= 0]
                strat_breakdown[str(s_name)] = {
                    "trades": int(len(s_trades)),
                    "win_rate_pct": float(round(len(s_w) / len(s_trades) * 100, 1)),
                    "net_pnl": float(round(float(s_trades["pnl"].sum()), 2)),
                    "profit_factor": float(round(float(s_w["pnl"].sum() / abs(s_l["pnl"].sum())), 2))
                                     if len(s_l) and s_l["pnl"].sum() != 0 else float("inf"),
                    "avg_r": float(round(float(s_trades["r"].mean()), 2)),
                }
        m["strategy_breakdown"] = strat_breakdown
    return m


def walk_forward(cfg: Config, panel: Dict[str, pd.DataFrame], universe: pd.DataFrame,
                 bench_feat: pd.DataFrame, folds: Optional[int] = None,
                 variant: str = "ANCHORED", exclude_final_holdout: bool = True,
                 start_date: Optional[str] = None, end_date: Optional[str] = None,
                 strategy: Optional[str] = None,
                 checkpoint_callback: Optional[Callable[[int, int, Dict[str, Any]], bool]] = None
                 ) -> List[Dict[str, Any]]:
    folds = int(folds or cfg.wf_folds)
    variant = variant.upper()
    dates = sorted(pd.Timestamp(x) for x in set().union(*[set(f.index) for f in panel.values() if len(f)]))
    elig = {}
    for s, f in panel.items():
        if len(f) > min(cfg.warmup_bars, 60):
            elig[s] = pd.Timestamp(f.index[min(cfg.warmup_bars, len(f) - 1)])
    if not dates or not elig:
        return []
    first_ok = min(elig.values())
    latest = dates[-1]

    req_start = pd.Timestamp(start_date) if start_date else (pd.Timestamp(cfg.bt_start) if cfg.bt_start else first_ok)
    req_end = pd.Timestamp(end_date) if end_date else (pd.Timestamp(cfg.bt_end) if cfg.bt_end else latest)
    end = min(latest, req_end)
    start = max(first_ok, req_start)
    if start >= end:
        start = first_ok

    # Check available date span before holdout
    total_span_days = (end - start).days
    if exclude_final_holdout and total_span_days >= 730:
        holdout_start = end - pd.DateOffset(months=cfg.holdout_months)
        candidates = [d for d in dates if start <= d < holdout_start]
        if candidates and len(candidates) > 120:
            end = candidates[-1]

    window = [d for d in dates if start <= d <= end]
    if len(window) < 60:
        window = [d for d in dates if d <= end]
    if len(window) < 40:
        return []

    min_test_bars = max(20, min(cfg.wf_min_test_bars, len(window) // (folds * 2)))
    if len(window) < folds * min_test_bars:
        folds = max(2, len(window) // max(min_test_bars, 1))

    split0 = max(min(cfg.warmup_bars, len(window) // 4), len(window) // 3)
    oos_dates = window[split0:]
    if len(oos_dates) < folds:
        oos_dates = window[len(window) // 4:]
    chunks = [list(x) for x in np.array_split(np.array(oos_dates, dtype="datetime64[ns]"), folds) if len(x)]

    LOG_BT.info(
        "Starting %d-Fold Purged Walk-Forward (Variant: %s, Strategy: %s) across %d bars",
        len(chunks), variant, strategy or "ALL", len(window)
    )

    bt = Backtester(cfg, panel, universe, bench_feat, strategy=strategy)
    results = []

    for i, raw in enumerate(chunks, 1):
        test = [pd.Timestamp(x) for x in raw]
        test_start, test_end = test[0], test[-1]
        pos_start = window.index(test_start)
        embargo = min(cfg.wf_embargo_bars, max(0, pos_start - 5))
        train_end_pos = max(0, pos_start - embargo - 1)
        train_end = window[train_end_pos]
        # Train on lookback (capped at 252 bars for fast, realistic walk-forward cross-validation)
        max_train_lookback = 252
        train_start_pos = 0 if (variant == "ANCHORED" and train_end_pos <= max_train_lookback) else max(0, train_end_pos - max_train_lookback)
        train_start = window[train_start_pos]

        LOG_BT.info(
            "Evaluating Fold %d/%d: Train [%s -> %s] -> Test [%s -> %s] (Embargo: %d bars)",
            i, len(chunks), train_start.date(), train_end.date(), test_start.date(), test_end.date(), embargo
        )

        # Out-of-sample test
        res_test = bt.run(str(test_start.date()), str(test_end.date()), verbose=False)
        m_test = res_test["metrics"]

        # In-sample train
        in_sharpe = 0.0
        try:
            if train_end_pos > 10:
                res_train = bt.run(str(train_start.date()), str(train_end.date()), verbose=False)
                in_sharpe = float(res_train["metrics"].get("sharpe", 0.0))
        except Exception:
            in_sharpe = 0.0

        out_sharpe = float(m_test.get("sharpe", 0.0))

        LOG_BT.info(
            "Fold %d/%d complete: In-Sample Sharpe: %.2f | OOS Sharpe: %.2f | CAGR: %.1f%% | MaxDD: %.1f%% | Trades: %s",
            i, len(chunks), in_sharpe, out_sharpe, m_test.get("cagr_pct", 0.0), m_test.get("max_dd_pct", 0.0), m_test.get("n_exits", 0)
        )

        fold_record = {
            "variant": variant, "fold": i,
            "train_start": str(train_start.date()), "train_end": str(train_end.date()),
            "embargo_bars": embargo,
            "test_start": str(test_start.date()), "test_end": str(test_end.date()),
            "eligible_symbols": int(sum(d <= test_start for d in elig.values())),
            "in_sample_sharpe": in_sharpe,
            "out_of_sample_sharpe": out_sharpe,
            "sharpe": out_sharpe,
            **{k: m_test.get(k) for k in (
                "cagr_pct", "max_dd_pct", "expectancy_r", "profit_factor",
                "n_exits", "avg_bars_held", "median_bars_held", "return_per_capital_day_bps")}
        }
        results.append(fold_record)
        if checkpoint_callback and not checkpoint_callback(i, len(chunks), fold_record):
            LOG_BT.warning("walk-forward interrupted at fold %d/%d", i, len(chunks))
            break

    avg_oos_sharpe = float(np.mean([r["out_of_sample_sharpe"] for r in results])) if results else 0.0
    LOG_BT.info(
        "Walk-Forward validation completed: %d folds evaluated | Mean Out-of-Sample Sharpe: %.2f",
        len(results), avg_oos_sharpe
    )

    return results
