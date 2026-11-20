from __future__ import annotations

import collections
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from app.core.config import CFG, Config
from app.core.database import db, now_ist
from app.engine.broker import make_broker
from app.engine.data_hub import SYNTH, DataHub
from app.engine.universe import build_universe

LOG_TASK = logging.getLogger("engine.tasks")


class TaskStatus:
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskManager:
    def __init__(self):
        self._threads: Dict[str, threading.Thread] = {}
        self._pause_events: Dict[str, threading.Event] = {}     # set() = running, clear() = paused
        self._cancel_events: Dict[str, threading.Event] = {}    # set() = cancel requested
        self._lock = threading.RLock()
        self._is_shutting_down: bool = False
        self._log_subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._recent_logs: collections.deque = collections.deque(maxlen=1000)

    def register_log_subscriber(self, callback: Callable[[Dict[str, Any]], None]):
        with self._lock:
            self._log_subscribers.append(callback)

    def unregister_log_subscriber(self, callback: Callable[[Dict[str, Any]], None]):
        with self._lock:
            if callback in self._log_subscribers:
                self._log_subscribers.remove(callback)

    def get_recent_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._recent_logs)
            return items[-limit:] if limit < len(items) else items

    def broadcast_log(self, level: str, message: str, source: str = "engine"):
        item = {
            "timestamp": now_ist().strftime("%H:%M:%S"),
            "level": level.upper(),
            "source": source,
            "message": message,
        }
        with self._lock:
            self._recent_logs.append(item)
            for cb in list(self._log_subscribers):
                try:
                    cb(item)
                except Exception:
                    pass

    def create_task(self, task_type: str, total_items: int = 0, total_stages: int = 1, initial_stage: str = "") -> str:
        task_id = f"TASK-{uuid.uuid4().hex[:8]}"
        now_str = now_ist().isoformat()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks (id, task_type, status, progress_pct, total_items, "
                "completed_items, stage, stage_num, total_stages, stage_progress_pct, "
                "message, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, task_type, TaskStatus.IDLE, 0.0, total_items, 0,
                 initial_stage or f"Stage 1/{total_stages}", 1, total_stages, 0.0,
                 f"Task {task_type} initialized", now_str, now_str)
            )
        self._pause_events[task_id] = threading.Event()
        self._pause_events[task_id].set()  # Not paused by default
        self._cancel_events[task_id] = threading.Event()
        return task_id

    def update_task_progress(self, task_id: str, completed: int, total: int,
                             current_item: str = "", message: str = "", checkpoint_data: Optional[Dict] = None,
                             stage: str = "", stage_num: int = 1, total_stages: int = 1):
        stage_pct = round((completed / max(total, 1)) * 100, 1)
        if total_stages > 1:
            overall_pct = round(((stage_num - 1) / total_stages + (completed / max(total, 1)) / total_stages) * 100, 1)
        else:
            overall_pct = stage_pct
        overall_pct = min(max(overall_pct, 0.0), 100.0)

        now_str = now_ist().isoformat()
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET completed_items = ?, total_items = ?, progress_pct = ?, "
                "current_item = ?, stage = ?, stage_num = ?, total_stages = ?, stage_progress_pct = ?, "
                "message = ?, updated_at = ? WHERE id = ?",
                (completed, total, overall_pct, current_item, stage, stage_num, total_stages, stage_pct, message, now_str, task_id)
            )
            if checkpoint_data is not None:
                cp_str = json.dumps(checkpoint_data)
                conn.execute(
                    "INSERT INTO task_checkpoints (task_id, task_type, checkpoint_data, updated_at) "
                    "VALUES (?, (SELECT task_type FROM tasks WHERE id=?), ?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET checkpoint_data=excluded.checkpoint_data, updated_at=excluded.updated_at",
                    (task_id, task_id, cp_str, now_str)
                )
        stage_tag = f"[{stage}]" if stage else f"[Stage {stage_num}/{total_stages}]"
        self.broadcast_log("INFO", f"[{task_id}] {stage_tag} {stage_pct:.1f}% ({completed}/{total}) {message} {current_item}".strip(), source="task.progress")

    def set_task_status(self, task_id: str, status: str, error: Optional[str] = None, message: Optional[str] = None):
        now_str = now_ist().isoformat()
        with db.get_connection() as conn:
            if error:
                conn.execute(
                    "UPDATE tasks SET status = ?, error = ?, message = ?, updated_at = ? WHERE id = ?",
                    (status, error, message or error, now_str, task_id)
                )
            elif status == TaskStatus.COMPLETED:
                conn.execute(
                    "UPDATE tasks SET status = ?, message = COALESCE(?, message), progress_pct = 100.0, stage_progress_pct = 100.0, updated_at = ? WHERE id = ?",
                    (status, message, now_str, task_id)
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = ?, message = COALESCE(?, message), updated_at = ? WHERE id = ?",
                    (status, message, now_str, task_id)
                )
        self.broadcast_log("INFO" if not error else "ERROR", f"[{task_id}] State changed to {status}: {error or message or ''}", source="task.state")

    def pause_task(self, task_id: str) -> bool:
        if task_id in self._pause_events:
            self._pause_events[task_id].clear()
            self.set_task_status(task_id, TaskStatus.PAUSED, message="Task paused by user")
            LOG_TASK.info("Task %s paused", task_id)
            return True
        return False

    def resume_task(self, task_id: str) -> bool:
        if task_id in self._pause_events:
            self._pause_events[task_id].set()
            self.set_task_status(task_id, TaskStatus.RUNNING, message="Task resumed")
            LOG_TASK.info("Task %s resumed", task_id)
            return True
        return False

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self._cancel_events:
            self._cancel_events[task_id].set()
            if task_id in self._pause_events:
                self._pause_events[task_id].set()  # unpause so loop can exit
            self.set_task_status(task_id, TaskStatus.CANCELLED, message="Task cancelled by user")
            LOG_TASK.info("Task %s cancelled", task_id)
            return True
        # If task is an orphaned ghost from a prior server session, cancel in database directly
        with db.get_connection() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                self.set_task_status(task_id, TaskStatus.CANCELLED, message="Task cancelled by user")
                LOG_TASK.info("Orphaned task %s marked CANCELLED", task_id)
                return True
        return False

    def delete_task(self, task_id: str) -> bool:
        """Deletes a single task from history."""
        if task_id in self._cancel_events:
            self.cancel_task(task_id)
        with db.get_connection() as conn:
            conn.execute("DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,))
            res = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return res.rowcount > 0

    def clear_tasks(self, status_in: Optional[List[str]] = None) -> int:
        """Clears completed, cancelled, or failed tasks from history."""
        if not status_in:
            status_in = [TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED]
        placeholders = ",".join("?" for _ in status_in)
        with db.get_connection() as conn:
            conn.execute(f"DELETE FROM task_checkpoints WHERE task_id IN (SELECT id FROM tasks WHERE status IN ({placeholders}))", status_in)
            res = conn.execute(f"DELETE FROM tasks WHERE status IN ({placeholders})", status_in)
            count = res.rowcount
            LOG_TASK.info("Cleared %d inactive tasks from history", count)
            return count

    def reconcile_stale_tasks(self) -> None:
        """On startup, marks any tasks left as RUNNING or PAUSED from a prior session as CANCELLED."""
        now_str = now_ist().isoformat()
        with db.get_connection() as conn:
            res = conn.execute(
                "UPDATE tasks SET status = 'CANCELLED', message = 'Interrupted by server restart', "
                "updated_at = ? WHERE status IN ('RUNNING', 'PAUSED')",
                (now_str,)
            )
            if res.rowcount > 0:
                LOG_TASK.info("Reconciled %d orphaned task(s) to CANCELLED on startup", res.rowcount)

    def shutdown(self) -> None:
        """Cancel all running tasks and unblock workers on server shutdown."""
        self._is_shutting_down = True
        LOG_TASK.info("Shutting down TaskManager, signalling all active tasks...")
        for task_id, evt in list(self._cancel_events.items()):
            evt.set()
        for task_id, evt in list(self._pause_events.items()):
            evt.set()

    def check_resumable_condition(self, task_id: str) -> bool:
        """Helper to be called inside worker loops. Handles pausing and cancellation.
        Returns False if cancelled (caller should exit).
        """
        if self._cancel_events.get(task_id, threading.Event()).is_set():
            return False
        pause_evt = self._pause_events.get(task_id)
        if pause_evt:
            # Block until unpaused or cancelled
            while not pause_evt.is_set():
                if self._cancel_events.get(task_id, threading.Event()).is_set():
                    return False
                time.sleep(0.5)
        return True

    def _format_task_dict(self, d: Dict[str, Any]) -> Dict[str, Any]:
        d["task_id"] = d.get("id", "")
        d["completed_steps"] = d.get("completed_items", 0)
        d["total_steps"] = d.get("total_items", 0)
        d["current_step"] = d.get("current_item") or d.get("message", "")
        d["error_message"] = d.get("error", "")
        d["stage"] = d.get("stage") or ""
        d["stage_num"] = d.get("stage_num") or 1
        d["total_stages"] = d.get("total_stages") or 1
        if d.get("status") == TaskStatus.COMPLETED:
            d["progress_pct"] = 100.0
            d["stage_progress_pct"] = 100.0
        else:
            st_pct = d.get("stage_progress_pct")
            d["stage_progress_pct"] = st_pct if st_pct is not None else d.get("progress_pct", 0.0)
        return d

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with db.get_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                d = self._format_task_dict(dict(row))
                cp = conn.execute("SELECT checkpoint_data FROM task_checkpoints WHERE task_id = ?", (task_id,)).fetchone()
                d["checkpoint"] = json.loads(cp["checkpoint_data"]) if cp else None
                return d
            return None

    def list_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        with db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._format_task_dict(dict(r)) for r in rows]


    # Concrete background task runners
    def start_universe_build_task(self, refresh: bool = True) -> str:
        task_id = self.create_task("UNIVERSE_BUILD", total_items=250, total_stages=3, initial_stage="Stage 1/3: Quotes & Liquidity Prefilter")

        def _worker():
            try:
                self.set_task_status(task_id, TaskStatus.RUNNING, message="Starting universe build")
                broker = make_broker(CFG)
                hub = DataHub(CFG, broker, SYNTH)

                def _cb(i, total, sym, extra=None):
                    if not self.check_resumable_condition(task_id):
                        return False
                    st_name = extra.get("stage", "Stage 1/3: Prefilter") if extra else "Stage 1/3: Prefilter"
                    st_num = extra.get("stage_num", 1) if extra else 1
                    st_total = extra.get("total_stages", 3) if extra else 3
                    msg = f"Processing {sym}"
                    self.update_task_progress(
                        task_id, i, total, current_item=sym, message=msg,
                        stage=st_name, stage_num=st_num, total_stages=st_total,
                        checkpoint_data={"last_symbol": sym, "index": i, "extra": extra}
                    )
                    return True

                df = build_universe(CFG, hub, broker, SYNTH, refresh=refresh, checkpoint_callback=_cb)
                if self._cancel_events.get(task_id, threading.Event()).is_set():
                    self.set_task_status(task_id, TaskStatus.CANCELLED, message="Universe build cancelled")
                else:
                    self.update_task_progress(
                        task_id, len(df), len(df), current_item="",
                        message=f"Universe build complete: {len(df)} symbols",
                        stage="Stage 3/3: Database Persistence", stage_num=3, total_stages=3
                    )
                    self.set_task_status(task_id, TaskStatus.COMPLETED, message=f"Built universe with {len(df)} tradeable names")
            except Exception as e:
                LOG_TASK.exception("Universe build task %s failed", task_id)
                self.set_task_status(task_id, TaskStatus.FAILED, error=str(e))

        t = threading.Thread(target=_worker, daemon=True)
        self._threads[task_id] = t
        t.start()
        return task_id

    def start_data_backfill_task(self, symbols: Optional[List[str]] = None, lookback_days: int = 1200) -> str:
        task_id = self.create_task("DATA_BACKFILL", total_stages=1, initial_stage="Stage 1/1: Historical Candle Sync")

        def _worker():
            try:
                self.set_task_status(task_id, TaskStatus.RUNNING, message="Backfilling historical candles")
                broker = make_broker(CFG)
                hub = DataHub(CFG, broker, SYNTH)

                sym_list = symbols
                if not sym_list:
                    with db.get_connection() as conn:
                        rows = conn.execute("SELECT symbol FROM instruments ORDER BY symbol").fetchall()
                        sym_list = [r["symbol"] for r in rows]
                if not sym_list:
                    # fallback to synthetic or smoke
                    sym_list = SYNTH.symbols

                total = len(sym_list)
                completed = 0
                for s in sym_list:
                    if not self.check_resumable_condition(task_id):
                        self.set_task_status(task_id, TaskStatus.CANCELLED, message="Backfill cancelled")
                        return
                    hub.get_daily(s, lookback_days=lookback_days, refresh=True)
                    completed += 1
                    self.update_task_progress(
                        task_id, completed, total, current_item=s,
                        message=f"Synced {s}", stage="Stage 1/1: Historical Candle Sync",
                        stage_num=1, total_stages=1, checkpoint_data={"completed_symbols": completed}
                    )
                self.set_task_status(task_id, TaskStatus.COMPLETED, message=f"Backfilled {completed} symbols")
            except Exception as e:
                LOG_TASK.exception("Backfill task %s failed", task_id)
                self.set_task_status(task_id, TaskStatus.FAILED, error=str(e))

        t = threading.Thread(target=_worker, daemon=True)
        self._threads[task_id] = t
        t.start()
        return task_id

    def start_backtest_task(self, folds: int = 6, variant: str = "ANCHORED") -> str:
        task_id = self.create_task("BACKTEST_RUN", total_items=folds, total_stages=folds, initial_stage=f"Fold 1/{folds}: Walk-Forward Validation")

        def _worker():
            try:
                self.set_task_status(task_id, TaskStatus.RUNNING, message="Initializing walk-forward backtest")
                broker = make_broker(CFG)
                hub = DataHub(CFG, broker, SYNTH)
                with db.get_connection() as conn:
                    rows = conn.execute("SELECT * FROM instruments").fetchall()
                    u_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
                if u_df.empty:
                    def _univ_bt_cb(i, total, sym, extra=None):
                        if not self.check_resumable_condition(task_id):
                            return False
                        st_desc = extra.get("stage", "Quotes Prefilter") if extra else "Quotes Prefilter"
                        self.update_task_progress(
                            task_id, i, total, current_item=sym,
                            message=f"{st_desc}: {sym} ({i}/{total})",
                            stage=f"Fold 1/{folds}: Loading Universe", stage_num=1, total_stages=folds
                        )
                        return True
                    u_df = build_universe(CFG, hub, broker, SYNTH, refresh=False, checkpoint_callback=_univ_bt_cb)
                if not self.check_resumable_condition(task_id):
                    self.set_task_status(task_id, TaskStatus.CANCELLED, message="Backtest cancelled")
                    return

                from app.engine.screener import compute_panel
                panel, bench_feat = compute_panel(CFG, hub, u_df, refresh=False)

                from app.engine.backtester import walk_forward

                def _cb(f_idx, total_folds, fold_data):
                    if not self.check_resumable_condition(task_id):
                        return False
                    self.update_task_progress(
                        task_id, f_idx, total_folds, current_item=f"Fold {f_idx}",
                        message=f"Fold {f_idx}/{total_folds} finished (Sharpe: {fold_data.get('sharpe', 0)})",
                        stage=f"Fold {f_idx}/{total_folds}: Walk-Forward Validation",
                        stage_num=f_idx, total_stages=total_folds,
                        checkpoint_data={"fold": f_idx, "fold_result": fold_data}
                    )
                    return True

                results = walk_forward(CFG, panel, u_df, bench_feat, folds=folds,
                                       variant=variant, checkpoint_callback=_cb)
                if self._cancel_events.get(task_id, threading.Event()).is_set():
                    self.set_task_status(task_id, TaskStatus.CANCELLED, message="Backtest cancelled")
                else:
                    self.set_task_status(task_id, TaskStatus.COMPLETED,
                                         message=f"Walk-forward completed {len(results)} folds")
            except Exception as e:
                LOG_TASK.exception("Backtest task %s failed", task_id)
                self.set_task_status(task_id, TaskStatus.FAILED, error=str(e))

        t = threading.Thread(target=_worker, daemon=True)
        self._threads[task_id] = t
        t.start()
        return task_id

    def start_screener_task(self, refresh: bool = False, use_llm: bool = True) -> str:
        total_stages = 6 if (use_llm and getattr(CFG, "gemini_api_key", None)) else 5
        task_id = self.create_task(
            "SCREENER_RUN", total_items=100, total_stages=total_stages,
            initial_stage="Stage 1/5: Loading Universe"
        )

        def _worker():
            try:
                self.set_task_status(task_id, TaskStatus.RUNNING, message="Initializing screener scan")
                broker = make_broker(CFG)
                hub = DataHub(CFG, broker, SYNTH)

                # Stage 1: Universe
                self.update_task_progress(
                    task_id, 0, 1, current_item="Universe check",
                    message="Verifying small-cap universe constituents",
                    stage="Stage 1/5: Loading Universe", stage_num=1, total_stages=total_stages
                )
                with db.get_connection() as conn:
                    rows = conn.execute("SELECT * FROM instruments").fetchall()
                    u_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
                if u_df.empty:
                    def _univ_cb(i, total, sym, extra=None):
                        if not self.check_resumable_condition(task_id):
                            return False
                        st_desc = extra.get("stage", "Quotes Prefilter") if extra else "Quotes Prefilter"
                        self.update_task_progress(
                            task_id, i, total, current_item=sym,
                            message=f"{st_desc}: {sym} ({i}/{total})",
                            stage="Stage 1/5: Loading Universe", stage_num=1, total_stages=total_stages,
                            checkpoint_data={"last_symbol": sym, "index": i}
                        )
                        return True
                    u_df = build_universe(CFG, hub, broker, SYNTH, refresh=False, checkpoint_callback=_univ_cb)

                if not self.check_resumable_condition(task_id):
                    self.set_task_status(task_id, TaskStatus.CANCELLED, message="Screener cancelled")
                    return

                self.update_task_progress(
                    task_id, len(u_df), max(len(u_df), 1), current_item=f"{len(u_df)} symbols",
                    message=f"Loaded {len(u_df)} instruments",
                    stage="Stage 1/5: Loading Universe", stage_num=1, total_stages=total_stages
                )

                if not self.check_resumable_condition(task_id):
                    self.set_task_status(task_id, TaskStatus.CANCELLED, message="Screener cancelled")
                    return

                # Stage 2: Panel & Feature Computation
                from app.engine.screener import compute_panel, run_screener
                from app.engine.trade_planner import build_trade_plan
                from app.engine.llm_overlay import GeminiAnalyst

                def _panel_cb(i, total, sym):
                    if not self.check_resumable_condition(task_id):
                        return False
                    self.update_task_progress(
                        task_id, i, total, current_item=sym,
                        message=f"Computing technical indicators for {sym}",
                        stage="Stage 2/5: Computing Features", stage_num=2, total_stages=total_stages,
                        checkpoint_data={"last_symbol": sym, "index": i}
                    )
                    return True

                panel, bench_feat = compute_panel(CFG, hub, u_df, refresh=refresh, checkpoint_callback=_panel_cb)

                if not self.check_resumable_condition(task_id):
                    self.set_task_status(task_id, TaskStatus.CANCELLED, message="Screener cancelled")
                    return

                # Stage 3: Market Regime & Breadth
                self.update_task_progress(
                    task_id, 1, 1, current_item=CFG.benchmark,
                    message="Evaluating SmallCap 250 benchmark & 50-DMA breadth",
                    stage="Stage 3/5: Market Regime", stage_num=3, total_stages=total_stages
                )

                # Stage 4: Strategy Scanning & Filtering
                self.update_task_progress(
                    task_id, 0, len(panel), current_item="Strategy Bank",
                    message="Scanning 4 quantitative edges (VCP, Trend, Revert, Donchian)",
                    stage="Stage 4/5: Strategy Scanning", stage_num=4, total_stages=total_stages
                )
                res = run_screener(CFG, hub, u_df, refresh=False, panel=panel, bench_feat=bench_feat)
                candidates = res["candidates"]
                regime = res["regime"]
                self.update_task_progress(
                    task_id, len(candidates), max(len(candidates), 1),
                    current_item=f"{len(candidates)} candidates",
                    message=f"Identified {len(candidates)} setups across strategies",
                    stage="Stage 4/5: Strategy Scanning", stage_num=4, total_stages=total_stages
                )

                # Stage 5 (or 6): Optional LLM Review
                if use_llm and total_stages > 5:
                    self.update_task_progress(
                        task_id, 0, max(len(candidates), 1), current_item="Gemini LLM",
                        message="Consulting Gemini AI overlay for qualitative catalyst verification",
                        stage="Stage 5/6: AI Qualitative Review", stage_num=5, total_stages=total_stages
                    )
                    analyst = GeminiAnalyst(CFG)
                    if analyst.enabled and not candidates.empty:
                        candidates = analyst.review(candidates, regime)

                # Stage Final: Scoring, Sizing, and Storing Plans
                final_stage_num = total_stages
                self.update_task_progress(
                    task_id, 0, max(len(candidates), 1), current_item="Trade Planner",
                    message="Sizing positions with volatility & Indian equity friction model",
                    stage=f"Stage {final_stage_num}/{total_stages}: Scoring & Trade Plans",
                    stage_num=final_stage_num, total_stages=total_stages
                )

                plans = []
                with db.get_connection() as conn:
                    open_risk_val = conn.execute(
                        "SELECT coalesce(sum((avg_entry - cur_stop) * open_qty), 0.0) FROM trades "
                        "WHERE state NOT IN ('CLOSED', 'CANCELLED', 'EXPIRED', 'REJECTED')"
                    ).fetchone()[0]
                open_risk = float(open_risk_val)
                scan_date = now_ist().date().isoformat()

                for _, r in candidates.iterrows():
                    plan = build_trade_plan(r, CFG, CFG.equity, CFG.equity, regime, open_risk, scan_date)
                    if plan:
                        row = plan.to_row()
                        with db.get_connection() as conn:
                            conn.execute(
                                "INSERT INTO signals (id, scan_date, symbol, strategy, score, entry_type, entry_trigger, "
                                "stop, t1, t2, qty, risk_amount, r_per_share, regime, features, llm, status, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                "ON CONFLICT(id) DO UPDATE SET score=excluded.score, status=excluded.status",
                                (row["id"], row["scan_date"], row["symbol"], row["strategy"], row["score"],
                                 row["entry_type"], row["entry_trigger"], row["stop"], row["t1"], row["t2"],
                                 row["qty"], row["risk_amount"], row["r_per_share"], row["regime"],
                                 row["features"], row["llm"], "PROPOSED", row["created_at"])
                            )
                        plans.append(plan.to_dict())

                run_id = f"SCAN-{uuid.uuid4().hex[:8]}"
                with db.get_connection() as conn:
                    conn.execute(
                        "INSERT INTO scan_runs (id, scan_date, mode, candidates_count, regime, exposure_mult, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (run_id, scan_date, CFG.mode, len(plans), regime.state, regime.exposure_mult, now_ist().isoformat())
                    )

                self.update_task_progress(
                    task_id, len(plans), max(len(plans), 1), current_item=f"{len(plans)} plans",
                    message=f"Scan complete: {len(plans)} actionable trade plans generated",
                    stage=f"Stage {final_stage_num}/{total_stages}: Scoring & Trade Plans",
                    stage_num=final_stage_num, total_stages=total_stages
                )
                self.set_task_status(
                    task_id, TaskStatus.COMPLETED,
                    message=f"Scan finished: generated {len(plans)} trade setups (Regime: {regime.state})"
                )

                # Dispatch Telegram alert for screener completion
                try:
                    from app.engine.telegram import TelegramNotifier
                    notifier = TelegramNotifier(CFG)
                    notifier.notify_screener_completed(plans, regime)
                except Exception as tg_err:
                    LOG_TASK.warning("Telegram screener alert failed: %s", tg_err)


            except Exception as e:
                LOG_TASK.exception("Screener scan task %s failed", task_id)
                self.set_task_status(task_id, TaskStatus.FAILED, error=str(e))

        t = threading.Thread(target=_worker, daemon=True)
        self._threads[task_id] = t
        t.start()
        return task_id


task_manager = TaskManager()
