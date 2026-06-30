# -*- mode: python ; coding: utf-8 -*-
"""bih.spec — PyInstaller ONE-FOLDER build for the Blockchain Investigation Hub (P8, Windows-first).

    make package            # npm run build, then: pyinstaller --noconfirm --clean bih.spec
    -> dist/BIH/BIH.exe + dist/BIH/_internal/   (_internal == sys._MEIPASS at runtime)

Windowed (no console) for release; set BIH_BUILD_CONSOLE=1 (``make package-debug``) for a console build
that prints tracebacks while debugging a frozen failure.

CONFIRM-FIRST (CLAUDE.md §6) — confirmed EMPIRICALLY against the installed packages on 2026-06-29, not
guessed (these are runtime-import failures that only a real build surfaces):
  * PyInstaller 6.21.0 / Python 3.13. One-folder spec shape: Analysis -> PYZ -> EXE(exclude_binaries=True)
    -> COLLECT.  ``sys._MEIPASS`` at runtime is ``dist/BIH/_internal``.
  * Modules imported DYNAMICALLY (by string / entry point) that static analysis cannot see — discovered
    from each package's entry-point table (importlib.metadata.entry_points):
      - yoyo backend:    ``yoyo.backends.core.sqlite3``   (group 'yoyo.backends')     + yoyo metadata
      - keyring backend: ``keyring.backends.Windows``      (group 'keyring.backends')  + keyring metadata
      - uvicorn:         ``uvicorn.protocols|loops|lifespan.*`` (imported by name at serve time)
      - pywebview:       ``webview.platforms.edgechromium`` (Windows WebView2) via pythonnet/clr_loader
  * ``backend.app.main:app`` is imported by uvicorn FROM A STRING and the audit/connector modules are
    walked with importlib — so the WHOLE ``backend`` package is collected (else it is silently dropped).
  * certifi's ``cacert.pem`` is collected for frozen TLS (see backend/app/runtime.py::configure_tls).

The datas list mirrors ``backend/app/app_paths.py::BUNDLED_RESOURCES`` EXACTLY: every resource lands at
the SAME relative path ``resource_path(rel)`` expects under _MEIPASS. A mismatch here is the #1 frozen
failure, so each mapping is spelled out 1:1 with that dict.

NOT bundled (Option C — the report prints via the OS-installed Edge/Chrome, services/report_render.py):
Playwright/Chromium, plus the test/dev stack and unused GUI toolkits — see ``excludes`` (keeps the
package ~50-90 MB, not ~350 MB+).
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

PROJECT = Path(SPECPATH).resolve()  # noqa: F821 - SPECPATH is injected by PyInstaller (the spec's dir)

# CONFIRM-FIRST / empirical (2026-06-29): this repo's base Python is a CONDA install, which keeps shared
# DLLs (sqlite3.dll, libssl/libcrypto, ffi, expat, bz2 — backing the stdlib _sqlite3/_ssl/_ctypes/pyexpat
# extensions) in ``<base_prefix>/Library/bin``, NOT in ``DLLs/``. PyInstaller's dependency scan does not
# search there, so it can't bundle them and the frozen app dies on ``import sqlite3`` / TLS. Putting that
# dir on PATH for the BUILD lets the scan resolve + bundle the full DLL chain. (On a vanilla python.org
# install this dir doesn't exist and this is a harmless no-op.)
_conda_dll = Path(sys.base_prefix) / "Library" / "bin"
if _conda_dll.is_dir():
    os.environ["PATH"] = str(_conda_dll) + os.pathsep + os.environ.get("PATH", "")


# --------------------------------------------------------------------------- read-only bundled resources
# Mirror app_paths.BUNDLED_RESOURCES: each file lands at the SAME relative path resource_path() expects.

def _tree(src_rel: str, dest_rel: str | None = None) -> list[tuple[str, str]]:
    """Every file under PROJECT/src_rel -> bundled at dest_rel/<subpath> (structure preserved)."""
    dest_rel = dest_rel or src_rel
    base = PROJECT / src_rel
    out: list[tuple[str, str]] = []
    for f in base.rglob("*"):
        if f.is_file():
            out.append((str(f), str(Path(dest_rel) / f.relative_to(base).parent)))
    return out


def _file(src_rel: str, dest_dir: str) -> list[tuple[str, str]]:
    """A single file -> bundled directory dest_dir."""
    return [(str(PROJECT / src_rel), dest_dir)]


datas: list[tuple[str, str]] = []
datas += _tree("frontend/dist", "frontend/dist")                       # BUNDLED_RESOURCES.frontend_dist
datas += _tree("backend/app/migrations", "backend/app/migrations")     # BUNDLED_RESOURCES.migrations (*.sql)
datas += _tree("backend/app/report_templates", "backend/app/report_templates")  # .report_templates
datas += _file("frontend/src/theme/tokens.json", "frontend/src/theme")          # .tokens_json
datas += _file("backend/app/normalization/data/graphsense_confidence.csv",      # .graphsense_confidence
               "backend/app/normalization/data")
datas += _tree("backend/app/intel", "backend/app/intel")                        # P8.7 intel snapshots

# certifi cacert.pem (== --collect-data certifi) — robust HTTPS for the connectors (runtime.py).
datas += collect_data_files("certifi")

# Package metadata for RUNTIME entry-point discovery (yoyo + keyring resolve backends via entry points).
datas += copy_metadata("yoyo-migrations")
datas += copy_metadata("keyring")


# --------------------------------------------------------------------------- hidden / dynamic imports
binaries: list = []
hiddenimports: list[str] = []

# The whole backend package: uvicorn imports "backend.app.main:app" from a STRING, and the audit runner +
# connector registry import modules with importlib — static analysis would miss all of them.
hiddenimports += collect_submodules("backend")

# uvicorn's protocol/loop/lifespan implementations are imported by name when the server starts.
hiddenimports += collect_submodules("uvicorn")

# yoyo's sqlite backend (the entry-point target) + the rest of the package.
hiddenimports += collect_submodules("yoyo")
hiddenimports += ["yoyo.backends.core.sqlite3"]

# keyring's OS backends (entry-point targets). runtime.configure_keyring() selects the right one frozen.
hiddenimports += collect_submodules("keyring.backends")

# pydantic v2 ships a compiled core + dynamic bits; collect to be safe (FastAPI request/response models).
hiddenimports += collect_submodules("pydantic")

# pywebview + its Windows WebView2 (edgechromium) backend via pythonnet/clr_loader. The WINDOWED app
# needs these (the human check); the headless --check gate never imports webview, so a webview gap does
# NOT fail the automated frozen smoke — it surfaces only when the window is opened.
_wv_datas, _wv_bins, _wv_hidden = collect_all("webview")
datas += _wv_datas
binaries += _wv_bins
hiddenimports += _wv_hidden
_cl_datas, _cl_bins, _cl_hidden = collect_all("clr_loader")
datas += _cl_datas
binaries += _cl_bins
hiddenimports += _cl_hidden
hiddenimports += ["clr_loader", "pythonnet"]

# Per-OS keyring backend (PyInstaller does not see keyring's entry-point backends). Windows is the
# deliverable now; macOS/Linux differ (documented in README "Build the desktop app").
if sys.platform == "win32":
    hiddenimports += ["keyring.backends.Windows"]
    _wv_datas2, _wv_bins2, _wv_hidden2 = collect_all("webview.platforms.edgechromium")
    datas += _wv_datas2
    binaries += _wv_bins2
    hiddenimports += _wv_hidden2
elif sys.platform == "darwin":
    hiddenimports += ["keyring.backends.macOS", "webview.platforms.cocoa"]
else:
    hiddenimports += ["keyring.backends.SecretService", "webview.platforms.gtk", "webview.platforms.qt"]


# --------------------------------------------------------------------------- excludes (keep it lean)
# NEVER bundle Playwright/Chromium: the report prints via the OS-installed Edge (services/report_render.py
# Option C). Also drop the test/dev stack and unused heavy GUI/sci toolkits a transitive import might pull.
excludes = [
    "playwright",
    "pytest", "_pytest", "pluggy", "hypothesis", "respx",
    "tkinter",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "matplotlib", "numpy", "pandas", "scipy", "IPython", "notebook", "jupyter",
]


# --------------------------------------------------------------------------- version-info resource (P9)
# Embed Win32 version-info (Company / Product / Version / Copyright / Description) into BIH.exe. A
# metadata-BLANK unsigned binary is a major SmartScreen/AV red-flag; filling these in makes the unsigned
# app as trustworthy-looking as it can be without a cert. The strings come from scripts/app_metadata.py
# (the SINGLE source the installer + signer also read) — loaded by FILE PATH so PyInstaller's analysis
# doesn't need the repo on sys.path. Windows-only; PyInstaller ignores ``version=`` on other OSes.
import importlib.util as _ilu

_meta_path = PROJECT / "scripts" / "app_metadata.py"
_meta_spec = _ilu.spec_from_file_location("bih_app_metadata", _meta_path)
_meta = _ilu.module_from_spec(_meta_spec)
_meta_spec.loader.exec_module(_meta)

_version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    _vt = _meta.VERSION_TUPLE  # 4-part (major, minor, patch, build)
    _version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_vt, prodvers=_vt, mask=0x3F, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            # 040904B0 == US-English (0x0409) + Unicode codepage (0x04B0 / 1200).
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", _meta.COMPANY_NAME),
                StringStruct("FileDescription", _meta.FILE_DESCRIPTION),
                StringStruct("FileVersion", _meta.VERSION),
                StringStruct("InternalName", _meta.EXE_NAME),
                StringStruct("LegalCopyright", _meta.COPYRIGHT),
                StringStruct("OriginalFilename", _meta.EXE_NAME + ".exe"),
                StringStruct("ProductName", _meta.APP_DISPLAY_NAME),
                StringStruct("ProductVersion", _meta.VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
        ],
    )


# --------------------------------------------------------------------------- assemble
CONSOLE = os.environ.get("BIH_BUILD_CONSOLE") == "1"  # make package-debug -> a console (traceback) build

a = Analysis(
    ["scripts/launch.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BIH",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # NEVER UPX-pack: a compressed/packed exe is a well-known AV false-positive trigger (the unpack stub
    # pattern-matches as malware to several engines). Keeping it unpacked trades a few MB for far less
    # AV friction on an UNSIGNED binary — see README "Distribution". (P9)
    upx=False,
    # Win32 version-info resource (Company/Product/Version/Copyright/Description) — None on non-Windows.
    version=_version_info,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # App/exe icon (committed at the repo root). Used for every package / package-debug build.
    icon=str(PROJECT / "8.ico") if (PROJECT / "8.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BIH",
)
