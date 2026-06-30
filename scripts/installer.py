"""Build the Windows installer for the one-folder app (P9). `make installer`.

Wraps Inno Setup's compiler (ISCC.exe) over ``installer/bih.iss`` to bundle ``dist/BIH/`` into a single
UNSIGNED ``dist/installer/BIH-Setup-<ver>.exe``:

  * per-user OR Program Files (the wizard asks), Start-Menu + Desktop shortcuts using 8.ico, a clean
    registered uninstaller, and uninstall LEAVES %APPDATA%\\BlockchainInvestigationHub intact (the .iss
    has no [UninstallDelete] for it).

No cert is required: after building, it attempts an OPTIONAL sign of the produced setup — which cleanly
SKIPS with an informational note when no cert is configured (see scripts/sign.py). So `make installer`
always succeeds on a bare machine and produces a working unsigned installer.

Confirm-first (CLAUDE.md §6): ISCC is located from PATH, the standard Program Files dirs, the per-user
winget location (``%LOCALAPPDATA%\\Programs\\Inno Setup 6``), and the Uninstall-registry InstallLocation.
If it's genuinely absent, we print the one-line winget command rather than guessing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Make sibling scripts importable whether run as `python scripts/installer.py` or imported as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_metadata as meta  # noqa: E402
import sign as signing  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIST_APP = ROOT / "dist" / "BIH"
OUT_DIR = ROOT / "dist" / "installer"
ISS = ROOT / "installer" / "bih.iss"
ICON = ROOT / "8.ico"

WINGET_HINT = "winget install --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements"


def find_iscc() -> str | None:
    """Locate ISCC.exe (the Inno Setup compiler) across PATH / Program Files / per-user / registry."""
    import shutil

    on_path = shutil.which("iscc") or shutil.which("ISCC")
    if on_path:
        return on_path
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 5" / "ISCC.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Last resort: read the Uninstall-registry InstallLocation (per-user + per-machine).
    loc = _iscc_from_registry()
    if loc:
        exe = Path(loc) / "ISCC.exe"
        if exe.exists():
            return str(exe)
    return None


def _iscc_from_registry() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, subkey in roots:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            name = winreg.QueryValueEx(sk, "DisplayName")[0]
                            if "Inno Setup" in name:
                                return winreg.QueryValueEx(sk, "InstallLocation")[0]
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def build_installer(iscc: str) -> Path:
    """Run ISCC over bih.iss with the volatile values passed as /D defines. Returns the setup.exe path."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    defines = {
        "MyAppVersion": meta.VERSION,
        "MySourceDir": str(DIST_APP),
        "MyOutputDir": str(OUT_DIR),
        "MyIconFile": str(ICON),
    }
    cmd = [iscc] + [f"/D{k}={v}" for k, v in defines.items()] + [str(ISS)]
    print(f">> building installer: {iscc}")
    print(f">>   {' '.join(cmd[1:])}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f">> ISCC failed (exit {proc.returncode}) — see the compiler output above.")
    setup = OUT_DIR / f"BIH-Setup-{meta.VERSION}.exe"
    if not setup.exists():
        raise SystemExit(f">> ISCC reported success but {setup} is missing — check OutputBaseFilename.")
    return setup


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Windows installer (Inno Setup) for dist/BIH.")
    ap.add_argument("--no-sign", action="store_true",
                    help="skip the optional post-build sign attempt entirely (always unsigned)")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print(">> the installer (Inno Setup) is Windows-only.", file=sys.stderr)
        return 2

    exe = DIST_APP / "BIH.exe"
    if not exe.exists():
        print(f">> {exe} not found — run `make package` first.", file=sys.stderr)
        return 2

    iscc = find_iscc()
    if not iscc:
        print(">> Inno Setup (ISCC.exe) not found. Install it, then re-run `make installer`:\n"
              f"     {WINGET_HINT}", file=sys.stderr)
        return 2

    setup = build_installer(iscc)
    size_mb = setup.stat().st_size / (1024 * 1024)

    # Optional sign — cleanly skips when no cert is configured (the build still succeeds unsigned).
    signed = False
    if not args.no_sign:
        try:
            result = signing.maybe_sign([setup])
            signed = bool(result.get("signed"))
        except RuntimeError as exc:
            # A cert WAS configured but signing failed — surface it, but the unsigned installer still exists.
            print(f">> WARNING: signing was configured but failed: {exc}", file=sys.stderr)

    print("\n" + "=" * 70)
    print(f">> installer: {setup}")
    print(f">> size: {size_mb:.0f} MB   signed: {'yes' if signed else 'no (unsigned)'}")
    print(">> shortcuts use 8.ico; uninstall preserves %APPDATA%\\BlockchainInvestigationHub (cases).")
    if not signed:
        print(">> unsigned: SmartScreen will show 'Unknown publisher' on download (expected — see README "
              "\"Distribution\"). To sign later: set BIH_SIGN_PFX+BIH_SIGN_PASSWORD (or BIH_SIGN_THUMBPRINT), "
              "then `make sign`.")
    print(">> next: run the installer, then `make smoke-frozen` against the installed copy.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
