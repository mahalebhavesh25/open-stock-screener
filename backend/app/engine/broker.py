from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.core.config import Config, mask_secret
from app.core.rate_limiter import retry

LOG_BROKER = logging.getLogger("engine.broker")
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist() -> datetime:
    return datetime.now(IST)

def _try(name: str):
    try:
        return __import__(name)
    except Exception:
        return None

growwapi = _try("growwapi")
pyotp = _try("pyotp")
HAS_GROWW = growwapi is not None


class StrEnum(str, Enum):
    """`str(TState.OPEN)` must be 'OPEN', not 'TState.OPEN' — the DB stores these values."""
    def __str__(self) -> str:
        return str(self.value)


class Txn(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrdType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"            # stop-loss limit
    SL_M = "SL_M"        # stop-loss market


class OrdStatus(StrEnum):
    NEW = "NEW"
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TRIGGER_PENDING = "TRIGGER_PENDING"


GROWW_STATUS_MAP = {
    "NEW": OrdStatus.NEW, "ACKED": OrdStatus.OPEN, "APPROVED": OrdStatus.OPEN,
    "OPEN": OrdStatus.OPEN, "TRIGGER_PENDING": OrdStatus.TRIGGER_PENDING,
    "EXECUTED": OrdStatus.FILLED, "COMPLETE": OrdStatus.FILLED,
    "CANCELLED": OrdStatus.CANCELLED, "CANCELLATION_REQUESTED": OrdStatus.CANCELLED,
    "REJECTED": OrdStatus.REJECTED, "FAILED": OrdStatus.REJECTED,
}


def describe_frame(df: Optional[pd.DataFrame], cols: Sequence[str] = ("close",)) -> str:
    """One-line summary of a bar frame for log messages."""
    if df is None:
        return "None"
    if len(df) == 0:
        return "EMPTY (0 rows)"
    try:
        span = f"{pd.Timestamp(df.index.min()).date()} → {pd.Timestamp(df.index.max()).date()}"
    except Exception:
        span = "non-date index"
    extra = []
    for c in cols:
        if c in df.columns:
            v = df[c].iloc[-1]
            extra.append(f"last_{c}={v:,.2f}" if isinstance(v, (int, float, np.floating)) and
                         np.isfinite(v) else f"last_{c}={v}")
    return f"{len(df)} rows · {span}" + (f" · {' '.join(extra)}" if extra else "")


class BrokerAdapter:
    """Interface. Every method returns plain dicts / DataFrames — no SDK objects leak out."""
    name = "base"

    def connect(self) -> None: ...
    def instruments(self) -> pd.DataFrame: raise NotImplementedError
    def daily_candles(self, symbol: str, start: date, end: date) -> pd.DataFrame: raise NotImplementedError
    def ltp(self, symbols: Sequence[str]) -> Dict[str, float]: raise NotImplementedError
    def quote(self, symbol: str) -> Dict[str, Any]: raise NotImplementedError
    def place_order(self, **kw) -> Dict[str, Any]: raise NotImplementedError
    def modify_order(self, order_id: str, **kw) -> Dict[str, Any]: raise NotImplementedError
    def cancel_order(self, order_id: str) -> Dict[str, Any]: raise NotImplementedError
    def order_detail(self, order_id: str) -> Dict[str, Any]: raise NotImplementedError
    def positions(self) -> pd.DataFrame: raise NotImplementedError
    def holdings(self) -> pd.DataFrame: raise NotImplementedError


class GrowwBroker(BrokerAdapter):
    name = "groww"
    _shared_instruments: Optional[pd.DataFrame] = None
    _shared_instruments_time: float = 0.0
    _shared_lock = threading.Lock()

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = None
        self._token: str = ""
        self._token_exp: Optional[datetime] = None
        self._instruments: Optional[pd.DataFrame] = None
        self._lock = threading.Lock()
        self.symbol_overrides: Dict[str, str] = {}
        self._quote_keys_logged = False
        self._jwt_warned = False

    # ---------- auth ----------
    def _token_valid(self) -> bool:
        return bool(self._token) and self._token_exp is not None and now_ist() < self._token_exp

    def _expiry(self) -> datetime:
        """Groww access tokens die daily around 06:00 IST. Refresh a little before."""
        n = now_ist()
        exp = n.replace(hour=5, minute=45, second=0, microsecond=0)
        if n >= exp:
            exp += timedelta(days=1)
        return exp

    @staticmethod
    def _jwt_expiry(token: str) -> Optional[datetime]:
        """Read the `exp` claim of a JWT access token without verifying it (diagnostics only)."""
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
            return datetime.fromtimestamp(int(exp), IST) if exp else None
        except Exception:
            return None

    def connect(self) -> None:
        if not HAS_GROWW:
            raise RuntimeError("growwapi not installed: pip install growwapi pyotp")
        with self._lock:
            if self._token_valid() and self.api is not None:
                return
            GrowwAPI = growwapi.GrowwAPI
            if self.cfg.groww_access_token:
                self._token = self.cfg.groww_access_token
                how = "access token"
                jwt_exp = self._jwt_expiry(self._token)
                if jwt_exp is not None and jwt_exp <= now_ist() and not self._jwt_warned:
                    self._jwt_warned = True
                    LOG_BROKER.error("Groww access token EXPIRED at %s — every request will fail "
                                     "(401/unauthorised). Generate a fresh token, or use the "
                                     "API key + secret (TOTP) flow.", f"{jwt_exp:%Y-%m-%d %H:%M IST}")
                elif jwt_exp is not None:
                    LOG_BROKER.info("access token expires %s (in %.1f h)",
                                    f"{jwt_exp:%Y-%m-%d %H:%M IST}",
                                    (jwt_exp - now_ist()).total_seconds() / 3600)
            elif self.cfg.groww_api_key and self.cfg.groww_api_secret:
                how = "api key + secret (TOTP)"
                LOG_BROKER.info("requesting a Groww access token via key + secret ...")
                self._token = GrowwAPI.get_access_token(
                    api_key=self.cfg.groww_api_key, secret=self.cfg.groww_api_secret)
            else:
                raise RuntimeError("no Groww credentials configured (set GROWW_ACCESS_TOKEN, or "
                                   "GROWW_API_KEY + GROWW_API_SECRET)")
            self.api = GrowwAPI(self._token)
            self._token_exp = self._expiry()
            jwt_exp = self._jwt_expiry(self._token)
            if jwt_exp is not None and now_ist() < jwt_exp < self._token_exp:
                self._token_exp = jwt_exp
            LOG_BROKER.info("Groww connected via %s; will reconnect after %s", how,
                            f"{self._token_exp:%Y-%m-%d %H:%M IST}")

    def _api(self):
        if not self._token_valid() or self.api is None:
            self.connect()
        return self.api

    # ---------- reference data ----------
    @retry(group="non_trading")
    def instruments(self) -> pd.DataFrame:
        if self._instruments is not None:
            return self._instruments
        with GrowwBroker._shared_lock:
            if GrowwBroker._shared_instruments is not None and (time.time() - GrowwBroker._shared_instruments_time < 43200):
                self._instruments = GrowwBroker._shared_instruments
                return self._instruments

        t0 = time.time()
        df = self._api().get_all_instruments()
        df = pd.DataFrame(df)
        df.columns = [c.lower() for c in df.columns]
        with GrowwBroker._shared_lock:
            GrowwBroker._shared_instruments = df
            GrowwBroker._shared_instruments_time = time.time()
        self._instruments = df
        LOG_BROKER.info("instrument master: %d rows in %.1fs (cached)", len(df), time.time() - t0)
        return df

    def find_index_instruments(self, terms: Sequence[str], exact: Sequence[str] = ()) -> pd.DataFrame:
        df = self.instruments().copy()
        cols = ["exchange", "segment", "trading_symbol", "groww_symbol", "name", "instrument_type"]
        for c in cols:
            if c not in df.columns:
                df[c] = ""
            df[c] = df[c].fillna("").astype(str).str.strip()
        hay = (df["trading_symbol"] + " | " + df["name"] + " | " + df["groww_symbol"]).str.upper()
        pat = "|".join(re.escape(t.upper()) for t in terms if t)
        m = hay.str.contains(pat, regex=True, na=False) & df["segment"].str.upper().eq("CASH")
        out = df.loc[m, cols].copy()
        if out.empty:
            return out
        norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
        targets = {norm(x) for x in exact if x}
        out["exact"] = out["trading_symbol"].map(norm).isin(targets) | out["name"].map(norm).isin(targets)
        out["is_index"] = out["instrument_type"].str.upper().isin(["IDX", "INDEX", "INDICES"])
        out["is_nse"] = out["exchange"].str.upper().eq("NSE")
        out["name_len"] = out["name"].str.len()
        out = out.sort_values(["exact", "is_index", "is_nse", "name_len"],
                              ascending=[False, False, False, True])
        return out.drop(columns=["name_len"]).reset_index(drop=True)

    def _groww_symbol(self, symbol: str, exchange: str = "NSE") -> str:
        return self.symbol_overrides.get(symbol) or f"{exchange}-{symbol}"

    # ---------- market data ----------
    @retry(group="historical")
    def _candle_window(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        api = self._api()
        gsym = self._groww_symbol(symbol)
        LOG_BROKER.debug("candles %s (%s) %s → %s", symbol, gsym, start, end)
        try:
            resp = api.get_historical_candles(
                exchange=api.EXCHANGE_NSE,
                segment=api.SEGMENT_CASH,
                groww_symbol=gsym,
                start_time=f"{start:%Y-%m-%d} 09:15:00",
                end_time=f"{end:%Y-%m-%d} 15:30:00",
                candle_interval=getattr(api, "CANDLE_INTERVAL_DAY", 1440),
            )
        except AttributeError:
            resp = api.get_historical_candle_data(
                trading_symbol=gsym.split("-", 1)[-1], exchange=api.EXCHANGE_NSE,
                segment=api.SEGMENT_CASH,
                start_time=f"{start:%Y-%m-%d} 09:15:00",
                end_time=f"{end:%Y-%m-%d} 15:30:00", interval_in_minutes=1440)
        if isinstance(resp, dict) and "payload" in resp:
            resp = resp["payload"]
        return self._candles_to_df(resp)

    def daily_candles(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        step = timedelta(days=int(self.cfg.groww_max_days_per_request))
        parts, cursor, n_req = [], start, 0
        while cursor <= end:
            stop = min(cursor + step - timedelta(days=1), end)
            df = self._candle_window(symbol, cursor, stop)
            if df is not None and not df.empty:
                parts.append(df)
            cursor = stop + timedelta(days=1)
            n_req += 1
        if not parts:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out = pd.concat(parts)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out

    @staticmethod
    def _candles_to_df(resp: Any) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if isinstance(resp, dict):
            resp = resp.get("payload", resp)
            resp = resp.get("candles", resp) if isinstance(resp, dict) else resp
        if resp is None or len(resp) == 0:
            return empty

        df = pd.DataFrame(resp)
        if isinstance(resp[0], dict):
            cols = {c.lower(): c for c in df.columns}
            ts_col = next((cols[k] for k in ("timestamp", "time", "date", "start_time", "epoch")
                           if k in cols), df.columns[0])
            ren = {ts_col: "ts"}
            for k in ("open", "high", "low", "close", "volume"):
                if k in cols:
                    ren[cols[k]] = k
            df = df.rename(columns=ren)
        else:
            if df.shape[1] < 5:
                return empty
            if df.shape[1] == 5:
                df.columns = ["ts", "open", "high", "low", "close"]
            else:
                df = df.iloc[:, :6]
                df.columns = ["ts", "open", "high", "low", "close", "volume"]
        if "ts" not in df.columns or "close" not in df.columns:
            return empty
        for k in ("open", "high", "low"):
            if k not in df.columns:
                df[k] = df["close"]
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

        ts = df["ts"]
        ts_num = pd.to_numeric(ts, errors="coerce")
        if ts_num.notna().all():
            unit = "ms" if float(ts_num.max()) > 1e12 else "s"
            dt = pd.to_datetime(ts_num, unit=unit, utc=True).dt.tz_convert(IST).dt.tz_localize(None)
        else:
            dt = pd.to_datetime(ts, errors="coerce", format="mixed")
            if getattr(dt.dt, "tz", None) is not None:
                dt = dt.dt.tz_convert(IST).dt.tz_localize(None)
        df["date"] = dt.dt.normalize()
        df = df.dropna(subset=["date"]).drop(columns=["ts"]).set_index("date")
        df = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["close"])
        return df[~df.index.duplicated(keep="last")].sort_index()

    @retry(group="live_data")
    def ltp(self, symbols: Sequence[str]) -> Dict[str, float]:
        api, out = self._api(), {}
        syms = list(symbols)
        for i in range(0, len(syms), 50):
            chunk = tuple(f"NSE_{s}" for s in syms[i:i + 50])
            r = api.get_ltp(segment=api.SEGMENT_CASH, exchange_trading_symbols=chunk)
            for k, v in (r or {}).items():
                out[k.replace("NSE_", "")] = float(v)
        return out

    @retry(group="live_data")
    def quote(self, symbol: str) -> Dict[str, Any]:
        api = self._api()
        q = api.get_quote(exchange=api.EXCHANGE_NSE, segment=api.SEGMENT_CASH, trading_symbol=symbol) or {}
        return {
            "symbol": symbol,
            "ltp": float(q.get("last_price", q.get("ltp", 0)) or 0),
            "open": float((q.get("ohlc") or {}).get("open", 0) or 0),
            "high": float((q.get("ohlc") or {}).get("high", 0) or 0),
            "low": float((q.get("ohlc") or {}).get("low", 0) or 0),
            "close": float((q.get("ohlc") or {}).get("close", 0) or 0),
            "volume": float(q.get("volume", 0) or 0),
            "upper_circuit": float(q.get("upper_circuit_limit", 0) or 0),
            "lower_circuit": float(q.get("lower_circuit_limit", 0) or 0),
            "bid": float(q.get("bid_price", 0) or 0),
            "ask": float(q.get("offer_price", q.get("ask_price", 0)) or 0),
            "market_cap_cr": float(q.get("market_cap", 0) or 0) / 1e7,
            "week_52_high": float(q.get("week_52_high", 0) or 0),
            "week_52_low": float(q.get("week_52_low", 0) or 0),
            "raw": q,
        }

    # ---------- orders ----------
    @retry(times=1, group="orders", idempotent=False)
    def place_order(self, symbol: str, txn: str, qty: int, order_type: str,
                    price: float = 0.0, trigger_price: float = 0.0,
                    ref_id: str = "", product: str = "CNC", **_) -> Dict[str, Any]:
        api = self._api()
        ot = {
            OrdType.MARKET: api.ORDER_TYPE_MARKET,
            OrdType.LIMIT: api.ORDER_TYPE_LIMIT,
            OrdType.SL: api.ORDER_TYPE_STOP_LOSS,
            OrdType.SL_M: api.ORDER_TYPE_STOP_LOSS_MARKET,
        }[OrdType(order_type)]
        payload = dict(
            trading_symbol=symbol,
            quantity=int(qty),
            validity=api.VALIDITY_DAY,
            exchange=api.EXCHANGE_NSE,
            segment=api.SEGMENT_CASH,
            product=api.PRODUCT_CNC if product == "CNC" else api.PRODUCT_MIS,
            order_type=ot,
            transaction_type=api.TRANSACTION_TYPE_BUY if txn == Txn.BUY else api.TRANSACTION_TYPE_SELL,
        )
        if price:
            payload["price"] = round(float(price), 2)
        if trigger_price:
            payload["trigger_price"] = round(float(trigger_price), 2)
        if ref_id:
            payload["order_reference_id"] = ref_id[:20]
        if self.cfg.dry_run:
            LOG_BROKER.warning("[DRY-RUN] would place: %s", payload)
            return {"order_id": f"DRY-{uuid.uuid4().hex[:12]}", "status": OrdStatus.NEW, "raw": payload}
        LOG_BROKER.warning("PLACING LIVE ORDER: %s", payload)
        r = api.place_order(**payload) or {}
        return {"order_id": r.get("groww_order_id", ""),
                "status": GROWW_STATUS_MAP.get(str(r.get("order_status", "")).upper(), OrdStatus.NEW),
                "raw": r}

    @retry(times=1, group="orders", idempotent=False)
    def modify_order(self, order_id: str, qty: Optional[int] = None, price: Optional[float] = None,
                     trigger_price: Optional[float] = None, order_type: Optional[str] = None) -> Dict[str, Any]:
        api = self._api()
        kw: Dict[str, Any] = {"groww_order_id": order_id, "segment": api.SEGMENT_CASH}
        if qty is not None:
            kw["quantity"] = int(qty)
        if price is not None:
            kw["price"] = round(float(price), 2)
        if trigger_price is not None:
            kw["trigger_price"] = round(float(trigger_price), 2)
        if order_type is not None:
            kw["order_type"] = order_type
        if self.cfg.dry_run:
            LOG_BROKER.warning("[DRY-RUN] would modify: %s", kw)
            return {"order_id": order_id, "status": OrdStatus.OPEN, "raw": kw}
        LOG_BROKER.warning("MODIFYING LIVE ORDER: %s", kw)
        return {"order_id": order_id, "raw": api.modify_order(**kw)}

    @retry(times=1, group="orders", idempotent=False)
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        api = self._api()
        if self.cfg.dry_run:
            LOG_BROKER.warning("[DRY-RUN] would cancel %s", order_id)
            return {"order_id": order_id, "status": OrdStatus.CANCELLED}
        LOG_BROKER.warning("CANCELLING LIVE ORDER %s", order_id)
        return {"order_id": order_id, "raw": api.cancel_order(groww_order_id=order_id, segment=api.SEGMENT_CASH)}

    @retry(group="orders")
    def order_detail(self, order_id: str) -> Dict[str, Any]:
        api = self._api()
        r = api.get_order_detail(groww_order_id=order_id, segment=api.SEGMENT_CASH) or {}
        return {
            "order_id": order_id,
            "status": GROWW_STATUS_MAP.get(str(r.get("order_status", "")).upper(), OrdStatus.PENDING),
            "filled_qty": int(r.get("filled_quantity", 0) or 0),
            "avg_price": float(r.get("average_fill_price", 0) or 0),
            "raw": r,
        }

    @retry(group="non_trading")
    def positions(self) -> pd.DataFrame:
        api = self._api()
        r = api.get_positions_for_user(segment=api.SEGMENT_CASH) or {}
        return pd.DataFrame(r.get("positions", r) or [])

    @retry(group="non_trading")
    def holdings(self) -> pd.DataFrame:
        r = self._api().get_holdings_for_user(timeout=5) or {}
        return pd.DataFrame(r.get("holdings", r) or [])


class PaperBroker(BrokerAdapter):
    """Deterministic simulator. Fills are resolved against the reference bar you feed it."""
    name = "paper"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cash = cfg.equity
        self.book: Dict[str, Dict[str, Any]] = {}
        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.marks: Dict[str, float] = {}
        self.bars: Dict[str, Dict[str, float]] = {}

    def connect(self) -> None:
        LOG_BROKER.info("PaperBroker ready | cash=₹%s", f"{self.cash:,.0f}")

    def set_bar(self, symbol: str, bar: Dict[str, float]) -> None:
        self.bars[symbol] = bar
        self.marks[symbol] = bar.get("close", self.marks.get(symbol, 0.0))

    def ltp(self, symbols: Sequence[str]) -> Dict[str, float]:
        return {s: self.marks.get(s, 0.0) for s in symbols}

    def quote(self, symbol: str) -> Dict[str, Any]:
        b = self.bars.get(symbol, {})
        return {"symbol": symbol, "ltp": self.marks.get(symbol, 0.0), **b}

    def place_order(self, symbol: str, txn: str, qty: int, order_type: str,
                    price: float = 0.0, trigger_price: float = 0.0, ref_id: str = "", **_):
        oid = f"PAP-{uuid.uuid4().hex[:12]}"
        LOG_BROKER.info("[paper] order %s: %s %s x%d %s px=%.2f trg=%.2f", oid, txn, symbol,
                        int(qty), order_type, price or 0.0, trigger_price or 0.0)
        self.open_orders[oid] = dict(order_id=oid, symbol=symbol, txn=txn, qty=int(qty),
                                     order_type=order_type, price=price, trigger_price=trigger_price,
                                     status=OrdStatus.OPEN, filled_qty=0, avg_price=0.0, ref_id=ref_id)
        return {"order_id": oid, "status": OrdStatus.OPEN, "raw": self.open_orders[oid]}

    def modify_order(self, order_id: str, qty=None, price=None, trigger_price=None, order_type=None):
        o = self.open_orders.get(order_id)
        if not o:
            return {"order_id": order_id, "status": OrdStatus.REJECTED}
        if qty is not None:
            o["qty"] = int(qty)
        if price is not None:
            o["price"] = price
        if trigger_price is not None:
            o["trigger_price"] = trigger_price
        if order_type is not None:
            o["order_type"] = order_type
        return {"order_id": order_id, "status": OrdStatus.OPEN, "raw": o}

    def cancel_order(self, order_id: str):
        o = self.open_orders.pop(order_id, None)
        if o:
            o["status"] = OrdStatus.CANCELLED
        return {"order_id": order_id, "status": OrdStatus.CANCELLED}

    def order_detail(self, order_id: str):
        o = self.open_orders.get(order_id, {"status": OrdStatus.FILLED, "filled_qty": 0, "avg_price": 0.0})
        return {"order_id": order_id, "status": o.get("status"),
                "filled_qty": o.get("filled_qty", 0), "avg_price": o.get("avg_price", 0.0), "raw": o}

    def match(self, symbol: str, bar: Dict[str, float], slip_bps: float) -> List[Dict[str, Any]]:
        fills = []
        o_, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        for oid, o in list(self.open_orders.items()):
            if o["symbol"] != symbol or o["status"] != OrdStatus.OPEN:
                continue
            slip = slip_bps / 10_000.0
            px = None
            if o["order_type"] == OrdType.MARKET:
                px = o_ * (1 + slip) if o["txn"] == Txn.BUY else o_ * (1 - slip)
            elif o["order_type"] == OrdType.LIMIT:
                if o["txn"] == Txn.BUY and l <= o["price"]:
                    px = min(o["price"], o_)
                elif o["txn"] == Txn.SELL and h >= o["price"]:
                    px = max(o["price"], o_)
            elif o["order_type"] in (OrdType.SL, OrdType.SL_M):
                trg = o["trigger_price"]
                if o["txn"] == Txn.BUY and h >= trg:
                    px = max(trg, o_) * (1 + slip)
                elif o["txn"] == Txn.SELL and l <= trg:
                    px = min(trg, o_) * (1 - slip)
            if px is None:
                continue
            qty = o["qty"]
            if o["txn"] == Txn.BUY:
                cost = px * qty
                if cost > self.cash:
                    qty = int(self.cash // px)
                    if qty <= 0:
                        o["status"] = OrdStatus.REJECTED
                        self.open_orders.pop(oid, None)
                        continue
                    cost = px * qty
                self.cash -= cost
                p = self.book.setdefault(symbol, {"qty": 0, "avg": 0.0})
                p["avg"] = (p["avg"] * p["qty"] + px * qty) / (p["qty"] + qty)
                p["qty"] += qty
            else:
                p = self.book.get(symbol, {"qty": 0, "avg": 0.0})
                qty = min(qty, p["qty"])
                if qty <= 0:
                    self.open_orders.pop(oid, None)
                    continue
                self.cash += px * qty
                p["qty"] -= qty
                if p["qty"] == 0:
                    self.book.pop(symbol, None)
            o.update(status=OrdStatus.FILLED, filled_qty=qty, avg_price=px)
            self.open_orders.pop(oid, None)
            fills.append({"order_id": oid, "symbol": symbol, "txn": o["txn"], "qty": qty,
                          "price": px, "purpose": o.get("ref_id", "")})
            LOG_BROKER.info("[paper] FILL %s %s x%d @ %.2f · cash now ₹%s", o["txn"], symbol, qty,
                            px, f"{self.cash:,.0f}")
        return fills

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame([{"symbol": s, **v, "ltp": self.marks.get(s, 0.0)}
                             for s, v in self.book.items()])

    def holdings(self) -> pd.DataFrame:
        return self.positions()


def make_broker(cfg: Config) -> BrokerAdapter:
    if cfg.mode == "LIVE":
        b = GrowwBroker(cfg)
    else:
        b = PaperBroker(cfg)
    b.connect()
    return b
