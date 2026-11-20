from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence, Set

import numpy as np
import pandas as pd

from app.core.config import CFG, Config
from app.core.database import db
from app.engine.broker import BrokerAdapter, GrowwBroker
from app.engine.data_hub import DEAD_SYMBOLS, HISTORY_FLOOR, DataHub, SyntheticMarket

LOG_UNIV = logging.getLogger("engine.universe")

NON_EQUITY_TOKENS = ("NCD", "BOND", "DEBENTURE", "SGB", "GOI", "GSEC", "TBILL", "ETF",
                     "MFUNIT", "REIT", "INVIT", "PARTLY", "WARRANT", "RIGHTS")


def load_exclusions(cfg: Config) -> set:
    ex = set()
    if os.path.exists(cfg.exclusion_file):
        try:
            df = pd.read_csv(cfg.exclusion_file)
            col = "symbol" if "symbol" in df.columns else df.columns[0]
            ex = set(df[col].astype(str).str.upper().str.strip())
            LOG_UNIV.info("loaded %d surveillance exclusions from %s", len(ex), cfg.exclusion_file)
        except Exception as e:
            LOG_UNIV.warning("exclusion file unreadable: %s", e)
    return ex


def load_mcap_map(path: str = "./mcap.csv") -> Dict[str, float]:
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            m = dict(zip(df.iloc[:, 0].astype(str).str.upper(), df.iloc[:, 1].astype(float)))
            LOG_UNIV.info("loaded %d market caps from %s", len(m), path)
            return m
        except Exception as e:
            LOG_UNIV.warning("failed reading %s: %s", path, e)
    return {}


def candidate_instruments(cfg: Config, broker: BrokerAdapter, synth: SyntheticMarket) -> pd.DataFrame:
    if cfg.data_source == "SYNTHETIC" or not isinstance(broker, GrowwBroker):
        LOG_UNIV.info("using synthetic instrument list (%d names)", len(synth.meta))
        return synth.meta.copy()

    df = broker.instruments().copy()
    df.columns = [c.lower() for c in df.columns]
    for c in ("exchange", "segment", "series", "instrument_type", "trading_symbol", "name", "isin"):
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].astype(str).str.strip()

    gates = [
        ("exchange == NSE", df["exchange"].str.upper().eq("NSE")),
        ("segment == CASH", df["segment"].str.upper().eq("CASH")),
        (f"series in {list(cfg.equity_series)}",
         df["series"].str.upper().isin([x.upper() for x in cfg.equity_series])),
        ("instrument_type is equity", df["instrument_type"].str.upper().isin(["EQ", "EQUITY", "STOCK", ""])),
        ("symbol not digit-led", ~df["trading_symbol"].str.match(r"^\d")),
        ("symbol well-formed", df["trading_symbol"].str.match(r"^[A-Z][A-Z0-9&\-]{1,19}$")),
        ("name not non-equity", ~df["name"].str.upper().str.contains("|".join(NON_EQUITY_TOKENS), na=False)),
        ("symbol not non-equity",
         ~df["trading_symbol"].str.upper().str.contains("|".join(NON_EQUITY_TOKENS), na=False)),
    ]
    keep = pd.Series(True, index=df.index)
    for label, g in gates:
        before = int(keep.sum())
        keep &= g.fillna(False).astype(bool)
        LOG_UNIV.info("  gate %-28s %6d → %6d", label, before, int(keep.sum()))
    if df["isin"].str.len().gt(0).any():
        before = int(keep.sum())
        keep &= (df["isin"].str.startswith("INE") | df["isin"].str.startswith("INF") | df["isin"].eq(""))
        LOG_UNIV.info("  gate %-28s %6d → %6d", "ISIN INE/INF", before, int(keep.sum()))
    for c in ("buy_allowed", "sell_allowed"):
        if c in df.columns:
            before = int(keep.sum())
            keep &= pd.to_numeric(df[c], errors="coerce").fillna(1).gt(0)
            LOG_UNIV.info("  gate %-28s %6d → %6d", c, before, int(keep.sum()))

    out = (df.loc[keep, [c for c in ("trading_symbol", "groww_symbol", "name", "series",
                                     "instrument_type", "exchange", "segment", "isin",
                                     "exchange_token", "tick_size", "lot_size")
                         if c in df.columns]]
             .rename(columns={"trading_symbol": "symbol"})
             .drop_duplicates("symbol")
             .reset_index(drop=True))
    LOG_UNIV.info("instrument master: %d rows → %d equities", len(df), len(out))
    return out


def prefilter_on_quotes(cfg: Config, broker: BrokerAdapter, symbols: Sequence[str],
                        checkpoint_callback: Optional[Callable[[int, int, str], bool]] = None) -> pd.DataFrame:
    rows, n, t0 = [], len(symbols), time.time()
    failed: List[str] = []
    zero_px: List[str] = []
    errors: Dict[str, int] = defaultdict(int)
    LOG_UNIV.info("stage 1 (quotes): %d symbols", n)
    for i, sym in enumerate(symbols, 1):
        if checkpoint_callback and not checkpoint_callback(i, n, sym):
            LOG_UNIV.warning("prefilter_on_quotes interrupted via callback at %d/%d", i, n)
            break
        try:
            q = broker.quote(sym)
        except Exception as e:
            failed.append(sym)
            errors[type(e).__name__] += 1
            continue
        px = float(q.get("ltp") or 0)
        if px <= 0:
            zero_px.append(sym)
            continue
        rows.append({"symbol": sym, "price": px,
                     "mcap_cr": float(q.get("market_cap_cr") or 0),
                     "day_turnover_cr": px * float(q.get("volume") or 0) / 1e7,
                     "day_volume": float(q.get("volume") or 0),
                     "week_52_high": float(q.get("week_52_high") or 0)})
        if i % 50 == 0 or i == n:
            el = time.time() - t0
            LOG_UNIV.info("quotes %d/%d (%.0f%%) · %.0fs elapsed · ok=%d failed=%d",
                          i, n, i / n * 100, el, len(rows), len(failed))
    return pd.DataFrame(rows)


def build_universe(cfg: Config, hub: DataHub, broker: BrokerAdapter,
                   synth: SyntheticMarket, refresh: bool = True,
                   checkpoint_callback: Optional[Callable[[int, int, str, dict], bool]] = None) -> pd.DataFrame:
    t_start = time.time()
    LOG_UNIV.info("building universe | source=%s prefilter=%s", cfg.data_source, cfg.prefilter_mode)
    inst = candidate_instruments(cfg, broker, synth)
    excl = load_exclusions(cfg)
    inst = inst[~inst["symbol"].str.upper().isin(excl)].copy()

    if "mcap_cr" not in inst.columns:
        inst["mcap_cr"] = np.nan
    mcap_map = load_mcap_map()
    if mcap_map:
        inst["mcap_cr"] = inst["symbol"].str.upper().map(mcap_map).fillna(inst["mcap_cr"])

    meta = inst.set_index("symbol")[["name"] + (["sector"] if "sector" in inst else [])].to_dict("index")
    use_quotes = cfg.prefilter_mode == "QUOTE" and cfg.data_source == "GROWW"

    if use_quotes:
        def _cb(i, n, sym):
            if checkpoint_callback:
                return checkpoint_callback(i, n, sym, {"stage": "Stage 1/3: Quotes Prefilter", "stage_num": 1, "total_stages": 3})
            return True
        pre = prefilter_on_quotes(cfg, broker, inst["symbol"].tolist(), checkpoint_callback=_cb)
        if pre.empty:
            return pre
        known = inst.set_index("symbol")["mcap_cr"]
        pre["mcap_src"] = pre.apply(
            lambda r: "csv" if pd.notna(known.get(r["symbol"]))
            else ("quote" if r["mcap_cr"] > 0 else "PROXY"), axis=1)
        pre["mcap_cr"] = pre.apply(
            lambda r: known.get(r["symbol"]) if pd.notna(known.get(r["symbol"]))
            else (r["mcap_cr"] if r["mcap_cr"] > 0 else r["day_turnover_cr"] * 900.0), axis=1)
        if not cfg.allow_mcap_proxy:
            pre = pre[pre["mcap_src"] != "PROXY"]
        pre = pre[pre["price"].between(cfg.price_min, cfg.price_max)
                  & pre["mcap_cr"].between(cfg.mcap_min_cr, cfg.mcap_max_cr)
                  & pre["day_turnover_cr"].ge(cfg.min_median_turnover_cr * 0.5)]
        pre = pre.sort_values("day_turnover_cr", ascending=False).head(cfg.max_universe)
    else:
        symbols = inst["symbol"].tolist()
        short = {}
        for i, s in enumerate(symbols, 1):
            if checkpoint_callback and not checkpoint_callback(i, len(symbols), s, {"stage": "Stage 1/3: Candle Prefilter", "stage_num": 1, "total_stages": 3}):
                break
            short[s] = hub.get_daily(s, lookback_days=cfg.prefilter_days, refresh=refresh)
        rows = []
        for sym, df in short.items():
            if df is None or len(df) < 40:
                continue
            px = float(df["close"].iloc[-1])
            med = float((df["close"] * df["volume"]).tail(20).median()) / 1e7
            mc = inst.set_index("symbol")["mcap_cr"].get(sym)
            rows.append({"symbol": sym, "price": px,
                         "mcap_cr": float(mc) if pd.notna(mc) else med * 900.0,
                         "day_turnover_cr": med, "day_volume": float(df["volume"].iloc[-1]),
                         "week_52_high": float(df["high"].max())})
        pre = pd.DataFrame(rows)
        if pre.empty:
            return pre
        pre = pre[pre["price"].between(cfg.price_min, cfg.price_max)
                  & pre["mcap_cr"].between(cfg.mcap_min_cr, cfg.mcap_max_cr)
                  & pre["day_turnover_cr"].ge(cfg.min_median_turnover_cr)]
        pre = pre.sort_values("day_turnover_cr", ascending=False).head(cfg.max_universe)

    pre = pre.reset_index(drop=True)
    LOG_UNIV.info("stage 1: %d names pass. Deep-fetching %d days history", len(pre), cfg.deep_history_days)

    # Stage 2: Deep history for survivors
    deep = {}
    total_pre = len(pre)
    for i, (_, r) in enumerate(pre.iterrows(), 1):
        sym = r["symbol"]
        if checkpoint_callback and not checkpoint_callback(i, total_pre, sym, {"stage": "Stage 2/3: Deep History Pull", "stage_num": 2, "total_stages": 3}):
            LOG_UNIV.warning("stage 2 interrupted at %d/%d", i, total_pre)
            break
        deep[sym] = hub.get_daily(sym, lookback_days=cfg.deep_history_days, refresh=refresh)

    rows = []
    for _, r in pre.iterrows():
        df = deep.get(r["symbol"])
        if df is None or len(df) < cfg.min_history_bars:
            continue
        med_turn_cr = float((df["close"] * df["volume"]).tail(20).median()) / 1e7
        if med_turn_cr < cfg.min_median_turnover_cr:
            continue
        m = meta.get(r["symbol"], {})
        rows.append({"symbol": r["symbol"], "name": m.get("name", r["symbol"]),
                     "sector": m.get("sector", "UNKNOWN"),
                     "price": float(df["close"].iloc[-1]), "mcap_cr": float(r["mcap_cr"]),
                     "median_turnover_cr": med_turn_cr,
                     "adv_shares": float(df["volume"].tail(20).median()), "bars": len(df)})

    u = pd.DataFrame(rows).sort_values("median_turnover_cr", ascending=False).reset_index(drop=True) \
        if rows else pd.DataFrame(columns=["symbol", "name", "sector", "price", "mcap_cr",
                                           "median_turnover_cr", "adv_shares", "bars"])
    db.set_setting("dead_symbols", sorted(DEAD_SYMBOLS))
    db.set_setting("history_floor", HISTORY_FLOOR)

    # Persist instruments to SQLite instruments table
    if not u.empty:
        with db.get_connection() as conn:
            for _, row in u.iterrows():
                conn.execute(
                    "INSERT INTO instruments (symbol, name, sector, mcap_cr, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sector=excluded.sector, "
                    "mcap_cr=excluded.mcap_cr, updated_at=excluded.updated_at",
                    (row["symbol"], row["name"], row["sector"], float(row["mcap_cr"]), time.strftime("%Y-%m-%d %H:%M:%S"))
                )
    LOG_UNIV.info("universe build complete: %d tradeable small caps in %.1fs", len(u), time.time() - t_start)
    return u
