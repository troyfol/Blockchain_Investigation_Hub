"""Single source of truth for the desktop app's identity + version (P9).

Read by THREE places that must agree, so none of them invents its own copy:
  * ``bih.spec``        — builds the Windows version-info resource embedded into ``BIH.exe`` (so the
    binary is not metadata-blank, a major SmartScreen/AV red flag).
  * ``scripts/installer.py`` — passes the name/version/publisher into Inno Setup (``/D`` defines).
  * ``scripts/sign.py``  — labels the signed artifact in its log lines.

Pure stdlib constants only — NO imports beyond the standard library — so the spec can load this by file
path during PyInstaller analysis without dragging the backend package onto the path.

CONFIRM-FIRST (CLAUDE.md §6): ``APP_ID`` MUST equal ``backend/app/app_paths.APP_NAME`` — it is the
``%APPDATA%\\<APP_ID>`` folder the installed app writes user data to, and the installer's uninstaller must
leave that folder intact. If you rename one, rename both (there is a test that pins this equality).
"""

from __future__ import annotations

# Display / window name (human-facing).
APP_DISPLAY_NAME = "Blockchain Investigation Hub"

# The per-OS app-data folder name. MUST match backend/app/app_paths.APP_NAME — the installed app writes
# %APPDATA%\BlockchainInvestigationHub and the uninstaller must never delete it (the user's cases live
# there). Pinned by test_app_metadata.py.
APP_ID = "BlockchainInvestigationHub"

# The frozen launcher exe basename (dist/BIH/BIH.exe), set by bih.spec's EXE(name=...).
EXE_NAME = "BIH"

# Version. v1.0.0 was the first public release (P10); v1.2.0 ships the R6 remediation + R6.1 upgrades.
# Bump here only — bih.spec + installer read this.
VERSION = "1.2.0"
# 4-part numeric tuple for the Win32 FIXEDFILEINFO (filevers/prodvers). Keep in lockstep with VERSION.
VERSION_TUPLE = (1, 2, 0, 0)

# Publisher / legal strings embedded in the exe AND shown by the installer + Programs-and-Features.
COMPANY_NAME = "Blockchain Investigation Hub Project"
PUBLISHER = COMPANY_NAME
COPYRIGHT = "Copyright (c) 2026 Blockchain Investigation Hub Project"
FILE_DESCRIPTION = "Blockchain Investigation Hub — provenance-first blockchain investigation & reporting hub"

# A STABLE installer identity GUID (Inno Setup AppId). NEVER change it across versions — Windows keys the
# upgrade/uninstall registration on this value, so changing it would orphan an installed copy. Generated
# once for P9; baked into installer/bih.iss as well (this constant documents it; the .iss is the consumer).
INSTALLER_APP_GUID = "7B1C0DE5-9A2B-4C3D-8E5F-0A1B2C3D4E5F"
