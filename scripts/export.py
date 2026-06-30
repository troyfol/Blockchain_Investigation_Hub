"""`make export CASE=...` — hash-manifest + zip a case folder to <case>.casefile (phase_10).

Usage: python scripts/export.py <path/to/case.db> [--verify]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.app.services.export import export_case, verify_casefile


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export a case as a verifiable .casefile bundle.")
    ap.add_argument("case_db", help="path to the case's case.db")
    ap.add_argument("--verify", action="store_true", help="re-open and validate the bundle after export")
    args = ap.parse_args(argv)

    db = Path(args.case_db)
    if not db.exists():
        print(f"case db not found: {db}", file=sys.stderr)
        return 2

    bundle = export_case(db.parent)
    print(f"exported {bundle}")

    if args.verify:
        report = verify_casefile(bundle)
        status = "OK" if report["ok"] else "FAILED"
        print(f"verify: {status}")
        if not report["ok"]:
            print(f"  manifest: {report['manifest']}", file=sys.stderr)
            print(f"  self_contained: {report['self_contained']}", file=sys.stderr)
            return 1
        sc = report["self_contained"]
        print(f"  files verified: {report['manifest']['file_count']}; "
              f"attached DBs: {len(sc['attached_databases'])}; audits: "
              f"{'pass' if sc['audits_passed'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
