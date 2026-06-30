"""`make setup` — create the venv, install deps, Playwright Chromium, frontend packages.

Cross-platform (Windows/Linux/macOS). Run by the base interpreter (e.g. `py -3.13`); it
creates a project-local `.venv` with that interpreter and installs everything into it.

Env knobs:
  BIH_SKIP_PLAYWRIGHT_BROWSERS=1  skip the ~150MB Chromium download (CI uses this).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def run(cmd: list[str], **kw) -> None:
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=kw.pop("cwd", ROOT), **kw)


def run_shell(cmd: str, cwd: Path) -> None:
    print(">>", cmd, f"(cwd={cwd})", flush=True)
    subprocess.run(cmd, check=True, cwd=str(cwd), shell=True)


def ensure_venv() -> None:
    if VENV_PY.exists():
        print(f"venv already present at {VENV}")
        return
    print(f"Creating venv at {VENV} using {sys.executable}")
    venv.EnvBuilder(with_pip=True, upgrade_deps=False).create(VENV)


def install_python_deps() -> None:
    run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(VENV_PY), "-m", "pip", "install", "-e", ".[dev]"])


def install_playwright() -> None:
    if os.environ.get("BIH_SKIP_PLAYWRIGHT_BROWSERS") == "1":
        print("BIH_SKIP_PLAYWRIGHT_BROWSERS=1 — skipping Chromium download.")
        return
    run([str(VENV_PY), "-m", "playwright", "install", "chromium"])


def install_frontend() -> None:
    npm = shutil.which("npm")
    frontend = ROOT / "frontend"
    if not npm:
        print("WARNING: npm not found on PATH — skipping frontend install. "
              "Install Node.js LTS, then `cd frontend && npm install`.")
        return
    # npm is a .cmd/.ps1 shim on Windows; run via the shell so it resolves correctly.
    run_shell("npm install", cwd=frontend)


def main() -> int:
    ensure_venv()
    install_python_deps()
    install_playwright()
    install_frontend()
    print("\nsetup complete.")
    print(f"  venv python: {VENV_PY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
