"""
Antigravity Small-Cap Swing Engine v2 - Unified Application Launcher
Starts FastAPI backend server hosting the compiled Angular frontend.
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
VENV_PYTHON = BACKEND_DIR / "venv" / "Scripts" / "python.exe"

def main():
    print("=" * 70)
    print("  ANTIGRAVITY // SMALL-CAP SWING TRADING ENGINE v2")
    print("  FastAPI Backend + Angular Dashboard + SQLite WAL Database")
    print("=" * 70)

    if not VENV_PYTHON.exists():
        print(f"[ERROR] Virtual environment Python not found at: {VENV_PYTHON}")
        sys.exit(1)

    print(f"\n[INFO] Starting Server on http://127.0.0.1:8000")
    print(f"[INFO] Access Swagger API Docs at: http://127.0.0.1:8000/docs")
    print(f"[INFO] Access Angular Dashboard at: http://127.0.0.1:8000/")
    print(f"[INFO] Press Ctrl+C to terminate.\n")

    cmd = [
        str(VENV_PYTHON),
        "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--reload"
    ]

    proc = subprocess.Popen(cmd, cwd=str(BACKEND_DIR))
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[INFO] Engine shutting down gracefully...")
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        print("[INFO] Engine stopped.")

if __name__ == "__main__":
    main()
