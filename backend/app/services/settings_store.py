"""Runtime settings overlay (P5): paid-connector enables, offline mode, and the cases folder.

These are the operator-tunable settings the in-app Settings UI changes at runtime — distinct from the
process-start ``config.Settings`` (env/.env) baseline and from API keys (which live ONLY in the OS
keyring, never here). Persisted to a small JSON in ``app_data_dir()`` so a toggle survives a restart;
read fresh on each access (no module cache) so test isolation is purely a function of
``BIH_APP_DATA_DIR`` and a switch takes effect immediately for in-process connectors.

NEVER stores a secret. Keys are write-only to the keyring (``secrets.py``); this file only ever holds
booleans + a path.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..app_paths import cases_root as _baseline_cases_root
from ..app_paths import settings_path

_lock = threading.RLock()


def _path() -> Path:
    return settings_path()  # under user_data_dir() (P7: frozen-safe, never under the bundle)


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}  # corrupt/garbage settings -> safe defaults, never crash the app


def _write(doc: dict) -> None:
    _path().write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- offline mode

def is_offline() -> bool:
    """Offline mode: when on, connectors refuse outbound network calls (ingest/expand disabled); all
    cached/ingested data + view/report/export keep working."""
    with _lock:
        return bool(_read().get("offline", False))


def set_offline(value: bool) -> None:
    with _lock:
        doc = _read()
        doc["offline"] = bool(value)
        _write(doc)


# --------------------------------------------------------------------------- paid connector enables

def paid_enabled_override(name: str) -> bool | None:
    """Runtime enable override for a paid connector, or ``None`` to fall back to the config default."""
    with _lock:
        return _read().get("paid_enabled", {}).get(name)


def set_paid_enabled(name: str, value: bool) -> None:
    with _lock:
        doc = _read()
        doc.setdefault("paid_enabled", {})[name] = bool(value)
        _write(doc)


# --------------------------------------------------------------------------- cases folder

def cases_root() -> Path:
    """The folder NEW cases are created under: the runtime override if set, else the P4 default
    (``BIH_CASES_ROOT`` env or ``cases/``). One place P5's 'change cases folder' flows through."""
    with _lock:
        cr = _read().get("cases_root")
        return Path(cr) if cr else _baseline_cases_root()


def set_cases_root(path: str | Path) -> None:
    with _lock:
        doc = _read()
        doc["cases_root"] = str(Path(path))
        _write(doc)


# --------------------------------------------------------------------------- intel source overrides (P8.7)

def intel_source(name: str) -> str | None:
    """An override path for an intel snapshot (``ofac`` / ``graphsense``), or ``None`` to use the bundled
    snapshot. Set by 'Refresh from source' (downloads to user-data) or a configurable override path."""
    with _lock:
        return _read().get("intel_sources", {}).get(name)


def set_intel_source(name: str, path: str | Path | None) -> None:
    with _lock:
        doc = _read()
        srcs = doc.setdefault("intel_sources", {})
        if path is None:
            srcs.pop(name, None)
        else:
            srcs[name] = str(Path(path))
        _write(doc)
