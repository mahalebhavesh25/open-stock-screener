from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.core.config import CFG, Config
from app.core.database import db, now_ist
from app.engine.broker import BrokerAdapter, GrowwBroker, describe_frame

LOG_DATA = logging.getLogger("engine.data")
LOG_BENCH = logging.getLogger("engine.benchmark")

OHLCV = ["open", "high", "low", "close", "volume"]

def _try(name: str):
    try:
        return __import__(name)
    except Exception:
        return None

yf = _try("yfinance")


class SyntheticMarket:
    """Reproducible fake NSE small-cap tape. Lets the engine run with no keys."""

    SECTORS = ["Chemicals", "Capital Goods", "IT Services", "Pharma", "Auto Anc",
               "Textiles", "Realty", "Financials", "FMCG", "Metals"]

    def __init__(self, n_symbols: int = 60, years: int = 5, seed: int = 7):
        self.rng = np.random.default_rng(seed)
        self.n, self.years = n_symbols, years
        self.symbols = [f"SC{i:03d}" for i in range(n_symbols)]
        self.dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=252 * years)
        self.meta = pd.DataFrame({
            "symbol": self.symbols,
            "name": [f"Smallcap Industries {i}" for i in range(n_symbols)],
            "sector": [self.SECTORS[i % len(self.SECTORS)] for i in range(n_symbols)],
            "mcap_cr": self.rng.uniform(600, 18_000, n_symbols).round(0),
            "series": "EQ", "instrument_type": "EQ", "exchange": "NSE", "segment": "CASH",
        })
        self._cache: Dict[str, pd.DataFrame] = {}
        self._bench: Optional[pd.DataFrame] = None

    def _path(self, seed_shift: int, drift_scale: float = 1.0) -> pd.DataFrame:
        rng = np.random.default_rng(1000 + seed_shift)
        n = len(self.dates)
        drift = np.zeros(n)
        i = 0
        while i < n:
            blk = int(rng.integers(70, 260))
            state = rng.choice([0.0022, 0.0002, -0.0016], p=[0.44, 0.31, 0.25])
            drift[i:i + blk] = state * drift_scale
            i += blk
        vol = np.abs(rng.normal(0.021, 0.006)) * drift_scale
        shock = rng.standard_t(4, n) / np.sqrt(2.0)
        ret = drift + vol * shock
        close = 100 * np.exp(np.cumsum(ret))
        close *= float(rng.uniform(0.4, 9.0))
        rng2 = np.random.default_rng(2000 + seed_shift)
        rng_hl = np.abs(rng2.normal(0.012, 0.006, n)) + 0.004
        open_ = close * (1 + rng2.normal(0, 0.004, n))
        high = np.maximum(open_, close) * (1 + rng_hl)
        low = np.minimum(open_, close) * (1 - rng_hl)
        base_vol = float(rng2.uniform(4e4, 9e5))
        vmult = 1 + 3.5 * np.clip(ret / (vol + 1e-9), 0, None) ** 1.5
        volume = base_vol * vmult * np.exp(rng2.normal(0, 0.35, n))
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "volume": volume.round()}, index=self.dates)

    def daily(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._cache:
            idx = self.symbols.index(symbol) if symbol in self.symbols else abs(hash(symbol)) % 997
            self._cache[symbol] = self._path(idx)
        return self._cache[symbol]

    def benchmark(self) -> pd.DataFrame:
        if self._bench is None:
            closes = pd.concat([self.daily(s)["close"] / self.daily(s)["close"].iloc[0]
                                for s in self.symbols], axis=1)
            idx = closes.mean(axis=1) * 1000
            self._bench = pd.DataFrame({"open": idx, "high": idx * 1.004,
                                        "low": idx * 0.996, "close": idx, "volume": 0.0})
        return self._bench


SYNTH = SyntheticMarket(n_symbols=max(CFG.smoke_symbols, 24), years=max(CFG.smoke_years, 4), seed=CFG.random_seed)


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV)


def load_candles_from_db(symbol: str, start_date: Optional[str] = None) -> pd.DataFrame:
    with db.get_connection() as conn:
        if start_date:
            cur = conn.execute(
                "SELECT date, open, high, low, close, volume FROM candles WHERE symbol = ? AND date >= ? ORDER BY date",
                (symbol, start_date)
            )
        else:
            cur = conn.execute(
                "SELECT date, open, high, low, close, volume FROM candles WHERE symbol = ? ORDER BY date",
                (symbol,)
            )
        rows = cur.fetchall()
        if not rows:
            return _empty_bars()
        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df = df.set_index("date")
        return df[OHLCV]


def save_candles_to_db(symbol: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    rows = []
    for dt, r in df.iterrows():
        date_str = pd.Timestamp(dt).date().isoformat()
        rows.append((symbol, date_str, float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r.get("volume", 0.0))))
    with db.get_connection() as conn:
        conn.executemany(
            "INSERT INTO candles (symbol, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, volume=excluded.volume",
            rows
        )


DEAD_SYMBOLS: set = set(db.get_setting("dead_symbols", []) or [])
HISTORY_FLOOR: Dict[str, str] = dict(db.get_setting("history_floor", {}) or {})

# Un-dead the benchmark if previously memorized
_PROTECTED = {CFG.benchmark, "NIFTY_SMLCAP_250"}
if DEAD_SYMBOLS & _PROTECTED:
    DEAD_SYMBOLS.difference_update(_PROTECTED)
    db.set_setting("dead_symbols", sorted(DEAD_SYMBOLS))


class DataHub:
    def __init__(self, cfg: Config, broker: BrokerAdapter, synth: SyntheticMarket):
        self.cfg, self.broker, self.synth = cfg, broker, synth
        self.protected: set = {cfg.benchmark}
        self.bench_source: str = "unresolved"
        self.bench_symbol: Optional[str] = None
        self.stats: Dict[str, int] = defaultdict(int)
        self._stats_lock = threading.Lock()

    def _count(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] += n

    @staticmethod
    def _yf_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        if yf is None:
            raise RuntimeError("yfinance not installed")
        d = yf.download(ticker, start=start, end=end + timedelta(days=1),
                        progress=False, auto_adjust=True, actions=True)
        if d is None or d.empty:
            return _empty_bars()
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.rename(columns=str.lower)
        if "volume" not in d.columns:
            d["volume"] = 0.0
        d = d[OHLCV]
        d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
        return d.dropna(subset=["close"])

    def _fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        empty = _empty_bars()
        protected = symbol in self.protected
        if symbol in DEAD_SYMBOLS and not protected:
            self._count("dead_skipped")
            return empty
        src = self.cfg.data_source
        self._count("provider_calls")
        if src == "SYNTHETIC":
            d = self.synth.daily(symbol)
            return d.loc[(d.index >= pd.Timestamp(start)) & (d.index <= pd.Timestamp(end))]
        try:
            if src == "GROWW":
                d = self.broker.daily_candles(symbol, start, end)
            elif src == "YFINANCE":
                d = self._yf_download(f"{symbol}.NS", start, end)
            elif src == "CSV":
                p = os.path.join(self.cfg.csv_dir, f"{symbol}.csv")
                if not os.path.exists(p):
                    return empty
                d = pd.read_csv(p, parse_dates=["date"]).set_index("date")
                d.columns = [c.lower() for c in d.columns]
                d = d[OHLCV]
            else:
                return empty
            if d is None or d.empty:
                self._count("provider_empty")
            return d if d is not None else empty
        except Exception as e:
            msg = str(e)
            self._count("provider_errors")
            if any(k in msg.lower() for k in ("not found", "invalid", "no data")) and not protected:
                DEAD_SYMBOLS.add(symbol)
                db.set_setting("dead_symbols", sorted(DEAD_SYMBOLS))
            (LOG_BENCH if protected else LOG_DATA).warning(
                "fetch failed %s (%s) %s → %s: %s", symbol, src, start, end, msg[:200])
            db.log_event("FETCH_FAIL", {"symbol": symbol, "error": msg[:300]})
            if self.cfg.allow_synthetic_fallback:
                d = self.synth.daily(symbol)
                return d.loc[(d.index >= pd.Timestamp(start)) & (d.index <= pd.Timestamp(end))]
        return empty

    def get_daily(self, symbol: str, lookback_days: int = 900, refresh: bool = True) -> pd.DataFrame:
        end = now_ist().date()
        start = end - timedelta(days=lookback_days)
        cached = load_candles_from_db(symbol, start.isoformat())
        parts = [cached] if not cached.empty else []

        if cached.empty:
            fresh = self._fetch(symbol, start, end)
            HISTORY_FLOOR[symbol] = min(HISTORY_FLOOR.get(symbol, "9999"), start.isoformat())
            db.set_setting("history_floor", HISTORY_FLOOR)
            if fresh is None or fresh.empty:
                self._count("empty_result")
                return cached
            save_candles_to_db(symbol, fresh)
            self._count("full_fetch")
            return fresh.sort_index()

        # Head gap
        touched = False
        head_have = cached.index.min().date()
        asked_before = HISTORY_FLOOR.get(symbol, "9999")
        if refresh and head_have > start + timedelta(days=7) and asked_before > start.isoformat():
            head = self._fetch(symbol, start, head_have - timedelta(days=1))
            HISTORY_FLOOR[symbol] = start.isoformat()
            db.set_setting("history_floor", HISTORY_FLOOR)
            self._count("head_fill")
            touched = True
            if head is not None and not head.empty:
                save_candles_to_db(symbol, head)
                parts.append(head)

        # Tail gap
        last = cached.index.max().date()
        if refresh and last < end - timedelta(days=1):
            tail = self._fetch(symbol, last - timedelta(days=3), end)
            self._count("tail_fill")
            touched = True
            if tail is not None and not tail.empty:
                save_candles_to_db(symbol, tail)
                parts.append(tail)
        if not touched:
            self._count("cache_only")

        if len(parts) == 1:
            return parts[0].sort_index()
        merged = pd.concat(parts)
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    def resolve_benchmark_symbol(self) -> Optional[Tuple[str, str, str]]:
        cfg = self.cfg
        if cfg.benchmark_groww_symbol:
            gs = cfg.benchmark_groww_symbol.strip()
            ts = gs.split("-", 1)[-1]
            return ts, gs, "config override"
        if not isinstance(self.broker, GrowwBroker):
            return None
        try:
            hits = self.broker.find_index_instruments(
                cfg.benchmark_search_terms,
                exact=(cfg.benchmark, "NIFTY SMALLCAP 250", "NIFTY SMLCAP 250"))
        except Exception as e:
            LOG_BENCH.error("instrument-master lookup failed: %s: %s", type(e).__name__, str(e)[:200])
            return None
        if hits.empty:
            return None
        best = hits.iloc[0]
        ts = str(best["trading_symbol"])
        gs = str(best["groww_symbol"]) or f"{best['exchange']}-{ts}"
        return ts, gs, f"{best['name'] or ts} [{best['instrument_type'] or '?'}]"

    def _validate_bench(self, df: Optional[pd.DataFrame]) -> Tuple[bool, str]:
        if df is None or df.empty:
            return False, "0 bars"
        if "close" not in df.columns or df["close"].dropna().empty:
            return False, "no close prices"
        n = int(df["close"].notna().sum())
        age = (now_ist().date() - pd.Timestamp(df.index.max()).date()).days
        desc = describe_frame(df)
        if n < self.cfg.min_benchmark_bars:
            return False, f"only {n} bars (< min_benchmark_bars={self.cfg.min_benchmark_bars}) · {desc}"
        return True, desc

    def benchmark(self, lookback_days: int = 900, refresh: bool = True) -> pd.DataFrame:
        cfg = self.cfg
        attempts: List[str] = []
        if cfg.data_source == "SYNTHETIC":
            b = self.synth.benchmark()
            self.bench_source, self.bench_symbol = "SYNTHETIC", "SYNTH_INDEX"
            return b.iloc[-min(len(b), lookback_days):]

        # 1. Groww
        if cfg.data_source == "GROWW":
            res = self.resolve_benchmark_symbol()
            if res:
                ts, gs, desc = res
                if isinstance(self.broker, GrowwBroker):
                    self.broker.symbol_overrides[ts] = gs
                self.protected.add(ts)
                DEAD_SYMBOLS.discard(ts)
                df = self.get_daily(ts, lookback_days, refresh=refresh)
                ok, why = self._validate_bench(df)
                attempts.append(f"GROWW {gs}: {why}")
                if ok:
                    self.bench_source, self.bench_symbol = f"GROWW:{gs}", ts
                    return df

        # 2. CSV
        if cfg.data_source == "CSV":
            df = self.get_daily(cfg.benchmark, lookback_days, refresh=refresh)
            ok, why = self._validate_bench(df)
            attempts.append(f"CSV {cfg.benchmark}.csv: {why}")
            if ok:
                self.bench_source, self.bench_symbol = f"CSV:{cfg.benchmark}", cfg.benchmark
                return df

        # 3. yfinance (only attempt if refresh is requested or no local cache exists)
        if refresh:
            end = now_ist().date()
            start = end - timedelta(days=lookback_days)
            for t in cfg.benchmark_yf_tickers:
                if yf is not None:
                    try:
                        df = self._yf_download(t, start, end)
                    except Exception:
                        df = _empty_bars()
                    ok, why = self._validate_bench(df)
                    attempts.append(f"YFINANCE {t}: {why}")
                    if ok:
                        self.bench_source, self.bench_symbol = f"YFINANCE:{t}", t
                        return df

        self.bench_source, self.bench_symbol = "UNAVAILABLE", None
        LOG_BENCH.error("benchmark UNAVAILABLE from every source: %s", ", ".join(attempts))
        return _empty_bars()

    def universe_proxy(self, raw: Dict[str, pd.DataFrame], min_bars: int) -> pd.DataFrame:
        closes = {s: d["close"] for s, d in raw.items() if d is not None and len(d) >= min_bars}
        if len(closes) < 10:
            return _empty_bars()
        px = pd.DataFrame(closes).sort_index()
        rets = px.pct_change(fill_method=None).clip(-0.20, 0.20)
        cnt = rets.notna().sum(axis=1)
        ew = rets.mean(axis=1, skipna=True)[cnt >= max(5, int(0.3 * len(closes)))].fillna(0.0)
        idx = 1000.0 * (1.0 + ew).cumprod()
        out = pd.DataFrame({"open": idx, "high": idx, "low": idx, "close": idx, "volume": 0.0})
        self.bench_source, self.bench_symbol = f"UNIVERSE_EW_PROXY({len(closes)} names)", None
        return out
