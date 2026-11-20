@echo off
title Antigravity Small-Cap Swing Engine v2
echo ======================================================================
echo   ANTIGRAVITY // SMALL-CAP SWING TRADING ENGINE v2 (Python 3.14)
echo   FastAPI Backend + Angular Frontend + SQLite WAL Engine
echo ======================================================================
echo.

cd /d "%~dp0\backend"
if exist "venv\Scripts\python.exe" (
    echo Starting server on http://127.0.0.1:8000 [Python 3.14] ...
    venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --timeout-graceful-shutdown 3
) else (
    echo [ERROR] Virtual environment not found at backend\venv.
    pause
)
