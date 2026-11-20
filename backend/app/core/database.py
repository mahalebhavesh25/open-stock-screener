from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from app.core.config import CFG

LOG_STORE = logging.getLogger("engine.store")
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist() -> datetime:
    return datetime.now(IST)

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, groww_symbol TEXT, exchange TEXT, segment TEXT,
    name TEXT, isin TEXT, series TEXT, instrument_type TEXT,
    exchange_token TEXT, tick_size REAL, lot_size INTEGER,
    sector TEXT, mcap_cr REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_candles_sym ON candles(symbol);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY, scan_date TEXT, symbol TEXT, strategy TEXT, score REAL,
    entry_type TEXT, entry_trigger REAL, stop REAL, t1 REAL, t2 REAL,
    qty INTEGER, risk_amount REAL, r_per_share REAL, regime TEXT,
    features TEXT, llm TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY, signal_id TEXT, symbol TEXT, strategy TEXT, state TEXT,
    side TEXT, planned_qty INTEGER, filled_qty INTEGER, open_qty INTEGER,
    avg_entry REAL, entry_trigger REAL, init_stop REAL, cur_stop REAL,
    t1 REAL, t2 REAL, trail_activated INTEGER, breakeven_moved INTEGER,
    realized_pnl REAL, max_favorable_excursion REAL, max_adverse_excursion REAL,
    created_at TEXT, closed_at TEXT, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, trade_id TEXT, symbol TEXT, broker_order_id TEXT,
    side TEXT, order_type TEXT, qty INTEGER, filled_qty INTEGER,
    price REAL, trigger_price REAL, status TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, payload TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    date TEXT PRIMARY KEY, starting_equity REAL, cash REAL, deployed_capital REAL,
    realized_pnl REAL, unrealized_pnl REAL, total_equity REAL, drawdown_pct REAL, peak_equity REAL
);

-- Extended tables for FastAPI application & Resumable Tasks
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL, -- IDLE, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED
    progress_pct REAL DEFAULT 0.0,
    total_items INTEGER DEFAULT 0,
    completed_items INTEGER DEFAULT 0,
    current_item TEXT,
    stage TEXT,
    stage_num INTEGER DEFAULT 1,
    total_stages INTEGER DEFAULT 1,
    stage_progress_pct REAL DEFAULT 0.0,
    message TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    checkpoint_data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id TEXT PRIMARY KEY,
    scan_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    candidates_count INTEGER DEFAULT 0,
    regime TEXT,
    exposure_mult REAL,
    created_at TEXT NOT NULL
);
"""

_db_lock = threading.RLock()


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or CFG.db_path
        self._ensure_init()

    def _ensure_init(self) -> None:
        p = Path(self.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            conn.executescript(SCHEMA)
            # Migrate any missing stage columns to existing tasks table
            try:
                task_cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
                if "stage" not in task_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN stage TEXT")
                if "stage_num" not in task_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN stage_num INTEGER DEFAULT 1")
                if "total_stages" not in task_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN total_stages INTEGER DEFAULT 1")
                if "stage_progress_pct" not in task_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN stage_progress_pct REAL DEFAULT 0.0")
            except Exception as e:
                LOG_STORE.warning("tasks migration warning: %s", e)
            # Enable WAL mode for high concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        LOG_STORE.info("Database initialized at %s", self.db_path)

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        with _db_lock:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_stats(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            counts = {}
            for table in ("instruments", "candles", "signals", "trades", "orders", "events", "equity", "tasks"):
                try:
                    c = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    counts[table] = c
                except Exception:
                    counts[table] = 0
            syms = conn.execute("SELECT count(DISTINCT symbol) FROM candles").fetchone()[0]
            span = conn.execute("SELECT min(date), max(date) FROM candles").fetchone()
            counts["candle_symbols"] = syms
            counts["candle_span"] = f"{span[0]} → {span[1]}" if span and span[0] else "None → None"
            return counts

    # Settings persistence
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except Exception:
                    return row["value"]
            return default

    def set_setting(self, key: str, value: Any) -> None:
        val_str = json.dumps(value) if not isinstance(value, str) else value
        now_str = now_ist().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, val_str, now_str)
            )

    # Event log
    def log_event(self, event_type: str, payload: Any) -> None:
        p_str = json.dumps(payload) if not isinstance(payload, str) else payload
        now_str = now_ist().isoformat()
        with self.get_connection() as conn:
            conn.execute("INSERT INTO events (event_type, payload, created_at) VALUES (?, ?, ?)",
                         (event_type, p_str, now_str))


db = Database()
