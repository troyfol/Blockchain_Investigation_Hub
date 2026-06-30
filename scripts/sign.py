"""Optional Authenticode code-signing — wired but NEVER required (P9). `make sign` / `make verify-sign`.

The whole build/installer pipeline must work end-to-end with ZERO cert configured. This script is the
drop-in that makes "ship a signed build" a one-line config change LATER, without touching anything else:

    set BIH_SIGN_PFX  = path to a .pfx  AND  BIH_SIGN_PASSWORD = its password     (file-based cert)
      — or —
    set BIH_SIGN_THUMBPRINT = a cert-store SHA-1 thumbprint                        (installed cert)

…then `make sign` (and `make installer`) start signing automatically. With NONE set, signing is cleanly
SKIPPED with an informational message and a non-error exit — so a cert is never needed to build.

Signs with signtool + an RFC3161 timestamp (so signatures outlive the cert's expiry). Targets default to
the inner launcher (dist/BIH/BIH.exe) plus any built installer(s) (dist/installer/*.exe).

Confirm-first (CLAUDE.md §6): we do NOT generate or require a self-signed cert (a self-signed signature is
no more trusted than unsigned and adds confusion). signtool is located from the Windows SDK; if a cert is
configured but signtool is missing, that IS an error (you asked to sign and we can't) — surfaced loudly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"  # RFC3161; override with BIH_SIGN_TIMESTAMP_URL


# --------------------------------------------------------------------------- config / discovery

def sign_config() -> dict | None:
    """The configured signing method, or ``None`` when nothing is set (-> signing is skipped).

    Returns ``{"method": "pfx", "pfx": ..., "password": ...}`` or ``{"method": "thumbprint", ...}``.
    """
    pfx = os.environ.get("BIH_SIGN_PFX")
    thumb = os.environ.get("BIH_SIGN_THUMBPRINT")
    if pfx:
        return {"method": "pfx", "pfx": pfx, "password": os.environ.get("BIH_SIGN_PASSWORD", "")}
    if thumb:
        return {"method": "thumbprint", "thumbprint": thumb}
    return None


def timestamp_url() -> str:
    return os.environ.get("BIH_SIGN_TIMESTAMP_URL", DEFAULT_TIMESTAMP_URL)


def find_signtool() -> str | None:
    """Locate signtool.exe: PATH first, then the newest x64 signtool in the Windows 10/11 SDK."""
    import shutil

    on_path = shutil.which("signtool")
    if on_path:
        return on_path
    roots = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin",
             Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Kits" / "10" / "bin"]
    candidates: list[Path] = []
    for base in roots:
        if base.is_dir():
            candidates += list(base.glob("*/x64/signtool.exe"))
            direct = base / "x64" / "signtool.exe"
            if direct.exists():
                candidates.append(direct)
    if not candidates:
        return None
    # Newest SDK version wins (the version is the parent-of-x64 dir name, e.g. 10.0.22621.0).
    def _ver_key(p: Path):
        name = p.parent.parent.name
        try:
            return tuple(int(x) for x in name.split("."))
        except ValueError:
            return (0,)

    return str(sorted(candidates, key=_ver_key)[-1])


# --------------------------------------------------------------------------- targets

def default_targets() -> list[Path]:
    """The inner launcher exe + any built installer exe(s) — whichever currently exist."""
    targets: list[Path] = []
    inner = DIST / "BIH" / "BIH.exe"
    if inner.exists():
        targets.append(inner)
    inst = DIST / "installer"
    if inst.is_dir():
        targets += sorted(inst.glob("*.exe"))
    return targets


# --------------------------------------------------------------------------- sign / verify

def _redacted(cmd: list[str], cfg: dict) -> str:
    pw = cfg.get("password")
    return " ".join(("***" if pw and a == pw else a) for a in cmd)


def sign_file(path: Path, cfg: dict, signtool: str) -> None:
    """Sign one file with signtool + an RFC3161 timestamp. Raises RuntimeError on failure."""
    cmd = [signtool, "sign", "/fd", "SHA256", "/tr", timestamp_url(), "/td", "SHA256"]
    if cfg["method"] == "pfx":
        cmd += ["/f", cfg["pfx"]]
        if cfg.get("password"):
            cmd += ["/p", cfg["password"]]
    else:
        cmd += ["/sha1", cfg["thumbprint"]]
    cmd.append(str(path))
    print(f">> signing {path.name}  ({_redacted(cmd, cfg)})", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"signtool failed for {path} (exit {proc.returncode}):\n"
                           f"{(proc.stdout or '').strip()}\n{(proc.stderr or '').strip()}")
    print(f">> signed {path.name} (timestamped via {timestamp_url()})")


def maybe_sign(paths: list[Path]) -> dict:
    """Sign each existing path IF a cert is configured; otherwise skip cleanly (no error).

    Returns ``{"signed": [...], "skipped": bool, "reason": str}``. Raises only when a cert IS configured
    but the act of signing genuinely fails (incl. signtool missing) — i.e. you asked to sign and we can't.
    """
    cfg = sign_config()
    if cfg is None:
        print(">> code-signing SKIPPED — no cert configured (set BIH_SIGN_PFX+BIH_SIGN_PASSWORD or "
              "BIH_SIGN_THUMBPRINT to enable). The build is intentionally UNSIGNED; this is not an error.")
        return {"signed": [], "skipped": True, "reason": "no cert configured"}
    signtool = find_signtool()
    if not signtool:
        raise RuntimeError("a signing cert is configured but signtool.exe was not found "
                           "(install the Windows SDK, or put signtool on PATH).")
    existing = [p for p in paths if p.exists()]
    if not existing:
        print(">> signing requested but no target files exist yet (build them first).")
        return {"signed": [], "skipped": False, "reason": "no targets"}
    print(f">> signing with {cfg['method']} cert via {signtool}")
    signed: list[str] = []
    for p in existing:
        sign_file(p, cfg, signtool)
        signed.append(str(p))
    return {"signed": signed, "skipped": False, "reason": ""}


def _is_signed(path: Path, signtool: str) -> bool | None:
    """True if Authenticode-signed, False if not signed, None if signtool couldn't be run."""
    proc = subprocess.run([signtool, "verify", "/pa", str(path)], capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    out = ((proc.stdout or "") + (proc.stderr or "")).lower()
    if "no signature" in out or "is not signed" in out:
        return False
    return None  # some other verify error — let the caller report it as a real failure


def verify(paths: list[Path]) -> int:
    """`make verify-sign`: run `signtool verify /pa` on each SIGNED target; skip unsigned ones informationally.

    Returns a process exit code: 0 unless a file that IS signed fails verification.
    """
    signtool = find_signtool()
    if not signtool:
        print(">> signtool not found — cannot verify. (No signing configured? Nothing to verify.)")
        return 0
    existing = [p for p in paths if p.exists()]
    if not existing:
        print(">> no built artifacts to verify.")
        return 0
    failed = 0
    any_signed = False
    for p in existing:
        signed = _is_signed(p, signtool)
        if signed is False:
            print(f">> {p.name}: UNSIGNED — skipped (verify only runs on signed files).")
            continue
        any_signed = True
        proc = subprocess.run([signtool, "verify", "/pa", "/v", str(p)], capture_output=True, text=True)
        if proc.returncode == 0:
            print(f">> {p.name}: signature VERIFIED.")
        else:
            failed += 1
            print(f">> {p.name}: signature verification FAILED:\n{(proc.stdout or '').strip()[-800:]}")
    if not any_signed:
        print(">> nothing signed yet — verify is a no-op (configure a cert + `make sign` to sign).")
    return 1 if failed else 0


# --------------------------------------------------------------------------- entrypoint

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Optional Authenticode signing (wired but never required).")
    ap.add_argument("--verify", action="store_true", help="verify signatures (make verify-sign) instead of signing")
    ap.add_argument("targets", nargs="*", help="files to sign/verify (default: dist/BIH/BIH.exe + dist/installer/*.exe)")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print(">> code-signing is Windows-only (signtool/Authenticode) — skipped on this OS.")
        return 0

    targets = [Path(t) for t in args.targets] if args.targets else default_targets()
    if args.verify:
        return verify(targets)
    try:
        maybe_sign(targets)
    except RuntimeError as exc:
        print(f">> signing FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
