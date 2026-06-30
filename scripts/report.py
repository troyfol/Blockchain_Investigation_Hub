"""`make report CASE=...` — generate an immutable report for a case (phase_09; render reworked in P3).

Usage: python scripts/report.py <path/to/case.db> [--title "..."]
Writes the self-contained report HTML (the hashed source of truth) and prints it to PDF using the OS
browser engine (Edge/WebView2 on Windows, system Chrome/Chromium elsewhere; Playwright optional), then
appends a frozen `report` row. With no engine the HTML report + frozen row are still produced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.app.db import get_connection
from backend.app.services.reporting import generate_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a case report PDF.")
    ap.add_argument("case_db", help="path to the case's case.db")
    ap.add_argument("--title", default="Investigation Report")
    args = ap.parse_args(argv)

    db = Path(args.case_db)
    if not db.exists():
        print(f"case db not found: {db} (run `make migrate CASE={db}` first)", file=sys.stderr)
        return 2

    conn = get_connection(db)
    try:
        result = generate_report(conn, case_dir=db.parent, title=args.title)
    finally:
        conn.close()

    print(f"report {result['report_id']}")
    print(f"  html:   {result['html_path']}  (hashed source of truth)")
    if result["pdf_path"] is not None:
        print(f"  pdf:    {result['pdf_path']}  (rendered via {result['engine']})")
    else:
        print(f"  pdf:    (skipped — {result.get('pdf_skip_reason') or 'no engine'}; "
              "HTML report is complete)")
    print(f"  sha256: {result['content_hash']}  (over the HTML)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
