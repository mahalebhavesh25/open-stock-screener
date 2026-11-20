from __future__ import annotations

import asyncio
import json
import queue
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.tasks.task_manager import task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/")
def list_tasks() -> Dict[str, Any]:
    tasks = task_manager.list_tasks()
    return {"tasks": tasks, "count": len(tasks)}



@router.get("/{task_id}")
def get_task_status(task_id: str) -> Dict[str, Any]:
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/{task_id}/pause")
def pause_task(task_id: str) -> Dict[str, Any]:
    ok = task_manager.pause_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot pause task (not running or doesn't exist)")
    return {"status": "paused", "task_id": task_id}


@router.post("/{task_id}/resume")
def resume_task(task_id: str) -> Dict[str, Any]:
    ok = task_manager.resume_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot resume task (not paused or doesn't exist)")
    return {"status": "resumed", "task_id": task_id}


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    ok = task_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot cancel task")
    return {"status": "cancelled", "task_id": task_id}


@router.delete("/{task_id}")
def delete_task(task_id: str) -> Dict[str, Any]:
    ok = task_manager.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted", "task_id": task_id}


@router.post("/clear-inactive")
def clear_inactive_tasks() -> Dict[str, Any]:
    count = task_manager.clear_tasks()
    return {"status": "cleared", "count": count}


@router.get("/logs/recent")
def get_recent_logs(limit: int = 100) -> Dict[str, Any]:
    logs = task_manager.get_recent_logs(limit=limit)
    return {"logs": logs, "count": len(logs)}


@router.get("/logs/stream")
async def stream_logs(request: Request):
    """SSE endpoint providing live system log stream to the frontend."""
    q: queue.Queue = queue.Queue(maxsize=200)

    def _sub(item: Dict[str, Any]):
        try:
            q.put_nowait(item)
        except queue.Full:
            pass

    task_manager.register_log_subscriber(_sub)

    async def _generator():
        try:
            # Yield initial heartbeat
            yield f"data: {json.dumps({'timestamp': 'INIT', 'level': 'INFO', 'source': 'system', 'message': 'Log stream connected to backend'})}\n\n"
            # Flush initial recent logs buffer so frontend immediately displays terminal history
            for r in task_manager.get_recent_logs(limit=60):
                yield f"data: {json.dumps(r)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                if getattr(task_manager, "_is_shutting_down", False):
                    break
                try:
                    # Non-blocking poll
                    item = q.get_nowait()
                    yield f"data: {json.dumps(item)}\n\n"
                except queue.Empty:
                    await asyncio.sleep(0.3)
        finally:
            task_manager.unregister_log_subscriber(_sub)

    return StreamingResponse(_generator(), media_type="text/event-stream")
