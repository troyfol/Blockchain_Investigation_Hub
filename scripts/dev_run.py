"""`make run` — start the FastAPI backend and the Vite frontend dev server together.

Both run until Ctrl-C. Backend on :8000, frontend on Vite's default :5173.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")


def main() -> int:
    if not VENV_PY.exists():
        print("venv not found — run `make setup` first.", file=sys.stderr)
        return 1

    procs: list[subprocess.Popen] = []

    print(">> starting backend on http://127.0.0.1:8000 (hot-reload on backend/app changes)")
    # Dev hot-reload, SCOPED to the Python source. `--reload-dir backend/app` keeps the watcher off the
    # churny dirs (cases/, raw_responses/, node_modules/, frontend/dist): watching the repo root would
    # thrash the reloader on case-data writes and silently miss real code edits (the stale-route bug).
    #
    # DEV ONLY. The packaged launcher (scripts/launch.py) must NEVER use --reload: it runs
    # uvicorn.Server(...).run(sockets=[sock]) in a daemon thread on a pre-bound socket, and its
    # splash/teardown/single-instance/ControlServer lifecycle owns that one process — --reload's
    # supervisor+worker subprocess split would break the socket handoff and the in-process teardown.
    procs.append(
        subprocess.Popen(
            [str(VENV_PY), "-m", "uvicorn", "backend.app.main:app",
             "--host", "127.0.0.1", "--port", "8000", "--reload", "--reload-dir", "backend/app"],
            cwd=str(ROOT),
        )
    )

    npm = shutil.which("npm")
    frontend = ROOT / "frontend"
    if npm and (frontend / "package.json").exists():
        print(">> starting frontend dev server (Vite)")
        procs.append(subprocess.Popen("npm run dev", cwd=str(frontend), shell=True))
    else:
        print("npm not found (or no frontend) — backend only. Install Node.js LTS for the UI.")

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("\nshutting down...")
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
