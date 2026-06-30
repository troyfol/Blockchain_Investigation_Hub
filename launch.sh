#!/usr/bin/env bash
# One-click launcher (macOS/Linux) — opens the Investigation Hub desktop window.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "venv not found - run 'make setup' first (needs Python 3.12+)." >&2
  exit 1
fi
exec .venv/bin/python scripts/launch.py "$@"
