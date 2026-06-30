"""Package the desktop app into a one-folder PyInstaller build (P8). `make package`.

Two steps, in order:
  1. ``npm run build`` (in ``frontend/``) so ``frontend/dist`` exists — it is bundled as a read-only
     resource at the path ``resource_path("frontend/dist")`` expects. Skipped if it is already built and
     ``--no-build`` is passed (CI re-runs).
  2. ``pyinstaller --noconfirm --clean bih.spec`` -> ``dist/BIH/`` (BIH.exe + _internal/).

Set ``BIH_BUILD_CONSOLE=1`` (``make package-debug``) for a console build that prints frozen tracebacks.

Then report where it landed and the total size, with a loud warning if the bundle is suspiciously large
(a sign Chromium/Playwright leaked in despite the spec excludes — the report is meant to print via the
OS-installed Edge, services/report_render.py, so no browser should be bundled).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "BIH"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def build_frontend(*, skip: bool) -> None:
    dist = ROOT / "frontend" / "dist"
    if skip and (dist / "index.html").exists():
        print(">> frontend/dist present, --no-build given — skipping npm run build")
        return
    print(">> building frontend (npm run build)…", flush=True)
    proc = subprocess.run("npm run build", cwd=str(ROOT / "frontend"), shell=True)
    if proc.returncode != 0:
        raise SystemExit(">> npm run build failed — fix the frontend build before packaging")
    if not (dist / "index.html").exists():
        raise SystemExit(">> npm run build did not produce frontend/dist/index.html")


def build_exe() -> None:
    print(">> running PyInstaller (bih.spec, one-folder)…", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "bih.spec"],
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise SystemExit(f">> PyInstaller failed (exit {proc.returncode})")


def report() -> None:
    exe = DIST / ("BIH.exe" if sys.platform == "win32" else "BIH")
    if not exe.exists():
        raise SystemExit(f">> build did not produce {exe} — check the PyInstaller output above")
    size_mb = _dir_size(DIST) / (1024 * 1024)
    print("\n" + "=" * 64)
    print(f">> packaged: {DIST}")
    print(f">> launcher: {exe}")
    print(f">> total size: {size_mb:.0f} MB")
    if size_mb > 200:
        print(">> WARNING: bundle is large (>200 MB) — did Chromium/Playwright leak in? "
              "The report prints via the OS Edge; no browser should be bundled. Check bih.spec excludes.")
    else:
        print(">> size OK (lean — no bundled Chromium).")
    print(">> next: `make smoke-frozen` (the frozen DoD gate), then double-click the launcher.")
    print("=" * 64)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the one-folder desktop app (PyInstaller).")
    ap.add_argument("--no-build", action="store_true",
                    help="skip npm run build if frontend/dist already exists")
    ap.add_argument("--console", action="store_true",
                    help="build a console exe that prints frozen tracebacks (debug; sets BIH_BUILD_CONSOLE)")
    args = ap.parse_args(argv)
    if args.console:
        os.environ["BIH_BUILD_CONSOLE"] = "1"  # the spec reads this -> console=True
        print(">> console (debug) build: BIH_BUILD_CONSOLE=1")
    build_frontend(skip=args.no_build)
    build_exe()
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
