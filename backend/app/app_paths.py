"""Central path resolution — the SINGLE source for every path, source-mode AND frozen (P7).

The app must work both running from source (dev / `make run`) and as a PyInstaller-frozen one-folder/
one-file app (P8 builds the exe; this phase just makes the code frozen-SAFE + testable via
simulated-frozen). Two kinds of path, kept strictly apart:

  * READ-ONLY BUNDLED RESOURCES (the built SPA, migrations, report templates, tokens.json, the vendored
    confidence.csv) live under ``bundle_dir()`` — ``sys._MEIPASS`` when frozen, the repo root in source.
    Resolve them with ``resource_path(rel)``. **Never write here**: in one-file mode ``_MEIPASS`` is a
    TEMP extraction dir wiped on exit, and in one-folder mode it is the read-only app bundle.
  * USER DATA (the case registry, settings.json, the single-instance lock, logs, and NEW case folders)
    lives under ``user_data_dir()`` — a portable dir next to the exe when a portable sentinel is present,
    else the per-OS app-data dir. ``cases_root()`` / ``settings_path()`` / ``logs_dir()`` resolve here.

CONFIRM-FIRST (CLAUDE.md §6) — confirmed against current PyInstaller docs:
  * ``sys.frozen`` is set to ``True`` by the PyInstaller bootloader (absent in source).
  * ``sys._MEIPASS`` is the absolute path to the bundle's resource root in BOTH one-file (a temp dir) and
    one-folder (the ``_internal`` dir) modes; absent in source. The canonical resource pattern is
    ``getattr(sys, "frozen", False)`` + ``sys._MEIPASS`` with a source-tree fallback.
  * The executable's own directory (``Path(sys.executable).parent``) is the place a "portable" build
    writes beside itself.
TODO: confirm at the P8 build that ``--add-data`` places each bundled resource at the SAME relative path
``resource_path`` expects (see ``BUNDLED_RESOURCES`` below), and that the keyring backends + certifi
data file are collected (see ``backend/app/runtime.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "BlockchainInvestigationHub"

# Portable mode: write user data NEXT TO THE EXE (a thumbdrive-friendly install) when either the env flag
# is set or a sentinel file sits beside the executable. Otherwise user data goes to the per-OS app-data
# dir (the installed-app location).
PORTABLE_ENV = "BIH_PORTABLE"
PORTABLE_SENTINEL = "portable.txt"

# The relative paths (under bundle_dir()) of every bundled READ-ONLY resource. Documented in ONE place so
# the P8 PyInstaller spec's --add-data can mirror exactly these source->dest mappings.
BUNDLED_RESOURCES = {
    "frontend_dist": "frontend/dist",
    "tokens_json": "frontend/src/theme/tokens.json",
    "migrations": "backend/app/migrations",
    "report_templates": "backend/app/report_templates",
    "graphsense_confidence": "backend/app/normalization/data/graphsense_confidence.csv",
    # P8.7 intel starter snapshots (OFAC SDN + GraphSense TagPack) — ship so "Check intel" works offline.
    "intel_ofac_sdn": "backend/app/intel/ofac_sdn.xml",
    "intel_graphsense_tagpack": "backend/app/intel/graphsense_tagpack.yaml",
}


# --------------------------------------------------------------------------- frozen detection / bundle

def is_frozen() -> bool:
    """True when running as a PyInstaller-frozen app (``sys.frozen`` set by the bootloader)."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """The root for READ-ONLY bundled resources: ``sys._MEIPASS`` when frozen (one-file temp extract OR
    one-folder ``_internal``), else the repo root in source. NEVER write under this path."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # Defensive: a frozen build with no _MEIPASS (unusual) -> the exe's own dir holds the resources.
        return Path(sys.executable).resolve().parent
    # Source mode: backend/app/app_paths.py -> parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


def resource_path(rel: str) -> Path:
    """Absolute path to a bundled read-only resource (``rel`` relative to ``bundle_dir()``)."""
    return bundle_dir() / rel


def exe_dir() -> Path:
    """The directory the running executable lives in (frozen) — where a portable build writes its data.
    In source mode this is the repo root (portable mode is a frozen concept)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- user data (writable)

def _portable() -> bool:
    if os.environ.get(PORTABLE_ENV) == "1":
        return True
    try:
        return (exe_dir() / PORTABLE_SENTINEL).exists()
    except OSError:
        return False


def user_data_dir() -> Path:
    """The per-user WRITABLE data root (registry, settings, lock, logs, new cases). Resolution order:

      1. ``BIH_APP_DATA_DIR`` env override (tests + power users) — highest, so tests never touch the real
         user dir;
      2. PORTABLE (env ``BIH_PORTABLE=1`` or a ``portable.txt`` beside the exe) -> ``<exe dir>/data``;
      3. per-OS app-data: Windows ``%APPDATA%/BIH`` (Roaming), macOS ``~/Library/Application Support/BIH``,
         Linux ``$XDG_DATA_HOME``/``~/.local/share``/BIH.

    Created on first use. This is the ONLY place the app writes; bundled resources are read-only.
    """
    override = os.environ.get("BIH_APP_DATA_DIR")
    if override:
        base: Path = Path(override)
    elif _portable():
        base = exe_dir() / "data"
    elif sys.platform == "win32":
        roaming = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        base = Path(roaming or (Path.home() / "AppData" / "Roaming")) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def app_data_dir() -> Path:
    """Back-compat alias for :func:`user_data_dir` (the P4/P5 name the registry + lock + settings use)."""
    return user_data_dir()


def cases_root() -> Path:
    """The default parent folder for NEW case folders. ``BIH_CASES_ROOT`` env wins (tests/dev); else when
    FROZEN it is ``user_data_dir()/cases`` (can't write under the bundle); else source-mode dev keeps the
    repo-relative ``cases/`` (matching ``cases/dev`` / ``cases/live``)."""
    env = os.environ.get("BIH_CASES_ROOT")
    if env:
        return Path(env)
    if is_frozen():
        return user_data_dir() / "cases"
    return Path("cases")


def default_cases_root() -> Path:
    """Back-compat alias for :func:`cases_root` (the P4/P5 name settings_store resolves through)."""
    return cases_root()


def settings_path() -> Path:
    """The runtime settings JSON (offline / paid-enables / cases-folder / theme), under user data."""
    return user_data_dir() / "settings.json"


def logs_dir() -> Path:
    """A writable logs dir under user data (created on use)."""
    d = user_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
