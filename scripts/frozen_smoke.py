"""Frozen end-to-end smoke — the P8 Definition-of-Done gate. `make smoke-frozen`.

Runs the BUILT exe (``dist/BIH/BIH.exe``) with the launcher's headless ``--check`` in a SANDBOXED
%APPDATA% and asserts the frozen app is correct WHERE source-mode tests cannot reach:

  * it starts and ``/health`` returns ok;
  * the case migrates and ``/api/graph`` returns 200 (the migrated DB reads back through frozen code);
  * the keyring backend RESOLVES (on Windows it must be available — Credential Manager);
  * TLS works — an httpx client builds against the bundled certifi CA (every HTTPS connector's init);
  * writes land under the per-OS app-data dir (``%APPDATA%/BlockchainInvestigationHub``) — proven by the
    real ``cases.json`` registry write — and NOTHING is written under ``_MEIPASS`` (``_internal``).

Sandbox, not pollution: ``APPDATA`` is pointed at a temp dir (and BIH_APP_DATA_DIR/CASES_ROOT/PORTABLE
unset) so the REAL ``%APPDATA%`` resolution branch is exercised without touching the user's real install.
The frozen process reports its own paths/probes on a ``SELFCHECK <json>`` line; this script asserts on
that plus the filesystem. Exit 0 = the frozen DoD gate passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The one-folder app dir to smoke. Defaults to the freshly-built dist/BIH; set BIH_SMOKE_DIST to point at
# an INSTALLED copy (P9: `make installer` -> install -> run the SAME frozen DoD gate on the installed app).
DIST = Path(os.environ["BIH_SMOKE_DIST"]) if os.environ.get("BIH_SMOKE_DIST") else ROOT / "dist" / "BIH"
EXE = DIST / ("BIH.exe" if sys.platform == "win32" else "BIH")
INTERNAL = DIST / "_internal"  # one-folder: sys._MEIPASS == this dir; the app must never write here


def _snapshot(path: Path) -> dict[str, tuple[int, float]]:
    """relpath -> (size, mtime) for every file under ``path`` (to prove nothing under it was written)."""
    out: dict[str, tuple[int, float]] = {}
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                st = f.stat()
                out[str(f.relative_to(path))] = (st.st_size, st.st_mtime)
    return out


def _run_check(case_db: Path, sandbox_appdata: Path) -> tuple[int, str, str]:
    env = dict(os.environ)
    # Sandbox the per-OS app-data resolution: point %APPDATA% at a temp dir and clear the overrides so the
    # frozen app resolves the REAL Windows %APPDATA%/<APP_NAME> branch — but inside our temp dir.
    env["APPDATA"] = str(sandbox_appdata)
    env["LOCALAPPDATA"] = str(sandbox_appdata)
    for k in ("BIH_APP_DATA_DIR", "BIH_CASES_ROOT", "BIH_PORTABLE", "BIH_CASE_DB"):
        env.pop(k, None)
    proc = subprocess.run(
        [str(EXE), "--check", "--no-build", "--host", "127.0.0.1", "--port", "0",
         "--case", str(case_db)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_selfcheck(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith("SELFCHECK "):
            try:
                return json.loads(line[len("SELFCHECK "):])
            except json.JSONDecodeError:
                return None
    return None


def main() -> int:
    if not EXE.exists():
        print(f">> {EXE} not found — run `make package` first.", file=sys.stderr)
        return 2

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    with tempfile.TemporaryDirectory(prefix="bih-frozen-smoke-") as td:
        tmp = Path(td)
        sandbox_appdata = tmp / "AppData"
        sandbox_appdata.mkdir()
        case_db = tmp / "case" / "case.db"

        before = _snapshot(INTERNAL)
        rc, out, err = _run_check(case_db, sandbox_appdata)
        after = _snapshot(INTERNAL)

        probes = _parse_selfcheck(out)

        # 1. exit code + self-check overall
        check("exe --check exited 0", rc == 0, f"exit={rc}\nstderr:\n{err.strip()[:800]}")
        check("SELFCHECK line emitted", probes is not None, "no SELFCHECK <json> on stdout")
        if probes is None:
            _report(checks, out, err)
            return 1
        check("self-check ok", probes.get("ok") is True, json.dumps(probes.get("graph", {})))

        # 2. health + migrated-case graph API
        check("/health status ok", probes.get("health_status") == "ok")
        graph = probes.get("graph", {})
        check("/api/graph returned 200", graph.get("http_status") == 200, json.dumps(graph))

        # 3. case actually migrated (the frozen migrate ran the bundled .sql)
        check("case.db created + migrated", case_db.exists(), str(case_db))

        # 4. frozen markers
        paths = probes.get("paths", {})
        check("running frozen (sys._MEIPASS set)", bool(paths.get("frozen")) and bool(paths.get("meipass")))

        # 5. writes land under the sandbox app-data dir, NOT under the bundle (_MEIPASS)
        udir = Path(paths.get("user_data_dir", "")) if paths.get("user_data_dir") else None
        meipass = Path(paths["meipass"]) if paths.get("meipass") else None
        check("user_data_dir under sandbox %APPDATA%",
              udir is not None and _is_under(udir, sandbox_appdata), str(udir))
        check("user_data_dir NOT under _MEIPASS",
              udir is not None and meipass is not None and not _is_under(udir, meipass),
              f"udir={udir} meipass={meipass}")
        check("cases_root resolves under user-data",
              _is_under(Path(paths.get("cases_root", "x")), sandbox_appdata), paths.get("cases_root"))
        registry = (udir / "cases.json") if udir else None  # the real write set_active_case makes
        check("registry (cases.json) written under app-data",
              registry is not None and registry.exists(), str(registry))

        # 6. NOTHING written under _internal (== _MEIPASS in one-folder)
        changed = _diff(before, after)
        check("no writes under _MEIPASS (_internal unchanged)", not changed,
              ("changed/new: " + ", ".join(sorted(changed))[:600]) if changed else "")

        # 7. keyring resolved (Windows: must be available; other OS: resolved-but-maybe-unavailable is ok)
        kr = probes.get("keyring", {})
        check("keyring backend resolved", "backend" in kr and "error" not in kr, json.dumps(kr))
        if sys.platform == "win32":
            check("keyring available (Windows Credential Manager)", kr.get("available") is True,
                  kr.get("backend", ""))

        # 8. TLS / certifi
        tls = probes.get("tls", {})
        check("TLS: bundled certifi CA exists + client builds", tls.get("exists") is True, json.dumps(tls))

    return _report(checks, out, err)


def _is_under(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _diff(before: dict, after: dict) -> set[str]:
    changed = {k for k in after if k not in before or after[k] != before.get(k)}
    changed |= {k for k in before if k not in after}
    return changed


def _report(checks: list[tuple[str, bool, str]], out: str, err: str) -> int:
    print("\n" + "=" * 70)
    print("FROZEN SMOKE — P8 Definition-of-Done gate")
    print("=" * 70)
    failed = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}")
        if not ok and detail:
            for ln in detail.splitlines():
                print(f"         {ln}")
        failed += not ok
    print("-" * 70)
    if failed:
        print(f">> {failed} check(s) FAILED. Frozen --check stdout (tail):")
        print("\n".join(out.splitlines()[-20:]))
        print(">> gate: FAILED")
        return 1
    print(f">> all {len(checks)} checks passed. gate: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
