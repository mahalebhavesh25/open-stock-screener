from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterable, Optional

LOG_INFRA = logging.getLogger("engine.infra")
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist() -> datetime:
    return datetime.now(IST)

# ---- NSE session windows (IST) --------------------------------------------
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
PREMARKET_SCAN = dtime(8, 45)
EOD_SCAN = dtime(15, 45)

NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-03-04", "2026-03-26", "2026-04-01", "2026-04-03",
    "2026-05-01", "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-21",
    "2026-11-09", "2026-12-25",
}


def is_trading_day(d: date, holidays: Iterable[str] = NSE_HOLIDAYS_2026) -> bool:
    return d.weekday() < 5 and d.isoformat() not in set(holidays)


def market_is_open(ts: Optional[datetime] = None) -> bool:
    ts = ts or now_ist()
    return is_trading_day(ts.date()) and MARKET_OPEN <= ts.time() <= MARKET_CLOSE


def next_trading_day(d: date) -> date:
    nd = d + timedelta(days=1)
    while not is_trading_day(nd):
        nd += timedelta(days=1)
    return nd


def get_market_status() -> dict:
    ts = now_ist()
    is_open = market_is_open(ts)
    is_today_trading = is_trading_day(ts.date())
    next_day = next_trading_day(ts.date())
    return {
        "current_time_ist": ts.isoformat(),
        "is_trading_day": is_today_trading,
        "is_market_open": is_open,
        "session": "LIVE" if is_open else ("PRE_MARKET" if is_today_trading and ts.time() < MARKET_OPEN else "CLOSED"),
        "next_trading_day": next_day.isoformat(),
    }
