"""`make clean` — remove build/test caches and stray bytecode.

Prunes build artifacts plus every ``__pycache__`` tree and ``*.egg-info`` under the repo, so a
stale orphan ``.pyc`` (a ``.pyc`` whose source was removed) can't cause a local pytest
collection error. Does NOT touch the venv, node_modules, or case data.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOP_LEVEL = ["build", "dist", ".pytest_cache", ".hypothesis", ".ruff_cache", ".mypy_cache"]
SKIP_DIRS = {".venv", "node_modules", ".git"}


def main() -> int:
    for name in TOP_LEVEL:
        shutil.rmtree(ROOT / name, ignore_errors=True)

    removed = 0
    for path in ROOT.rglob("__pycache__"):
        if SKIP_DIRS & set(path.parts):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    for path in ROOT.rglob("*.egg-info"):
        if SKIP_DIRS & set(path.parts):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1

    print(f"clean: removed top-level caches + {removed} __pycache__/egg-info dirs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
