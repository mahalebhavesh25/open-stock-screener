from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import backtest, data, overview, screener, settings, tasks, trades, universe
from app.core.config import CFG, Config
from app.core.database import db
from app.tasks.task_manager import task_manager

class TaskLogBroadcastHandler(logging.Handler):
    """Bridges standard python logging to task_manager.broadcast_log and SSE frontend stream."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Skip if coming from task broadcast itself to avoid infinite duplicate loop
            if record.name in ("task.progress", "task.state"):
                return
            msg = record.getMessage()
            task_manager.broadcast_log(
                level=record.levelname,
                message=msg,
                source=record.name
            )
        except Exception:
            pass

class EndpointFilter(logging.Filter):
    """Filters out routine frontend polling GET requests from flooding the console."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Suppress routine high-frequency polling
        for route in ("/api/overview/", "/api/tasks/", "/api/health"):
            if f"GET {route}" in msg and "200 OK" in msg:
                return False
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-17s | %(message)s",
    datefmt="%H:%M:%S"
)
root_logger = logging.getLogger()
root_logger.addHandler(TaskLogBroadcastHandler())
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Reconcile any orphaned tasks from previous sessions
    try:
        task_manager.reconcile_stale_tasks()
    except Exception as e:
        log.warning("Failed reconciling stale tasks: %s", e)

    # Load persisted settings if available
    try:
        saved_cfg = db.get_setting("engine_config")
        if saved_cfg and isinstance(saved_cfg, dict):
            for k, v in saved_cfg.items():
                if hasattr(CFG, k):
                    setattr(CFG, k, v)
            log.info("Loaded persisted configuration from database")
    except Exception as e:
        log.warning("Failed loading persisted settings: %s", e)
    log.info("Antigravity Swing Trading Engine Backend started on %s mode", CFG.run_mode)
    yield
    log.info("Antigravity Swing Trading Engine Backend shutting down...")
    try:
        task_manager.shutdown()
    except Exception as e:
        log.warning("Error during task manager shutdown: %s", e)


app = FastAPI(
    title="Small-Cap Swing Trading Engine v2 API",
    description="Production-grade REST & Real-time Engine for Indian Equities",
    version="2.0.0",
    lifespan=lifespan
)

# CORS configuration for Angular frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(overview.router)
app.include_router(universe.router)
app.include_router(data.router)
app.include_router(screener.router)
app.include_router(trades.router)
app.include_router(backtest.router)
app.include_router(settings.router)
app.include_router(tasks.router)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "2.0.0", "run_mode": CFG.run_mode, "execution": CFG.mode}


# Mount Static Frontend if built
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist" / "frontend" / "browser"

if frontend_dist.exists():
    app.mount("/browser", StaticFiles(directory=str(frontend_dist)), name="browser")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api/") or full_path == "api":
            return {"error": "API route not found"}
        target_file = frontend_dist / full_path
        if full_path and target_file.is_file():
            return FileResponse(target_file)
        return FileResponse(frontend_dist / "index.html")

