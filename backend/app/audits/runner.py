"""Audit runner (phase_00 step 5).

Discovers every ``@audit_check`` under ``backend.app.audits.checks``, runs each against a
case DB, prints a per-check PASS/FAIL line plus offending rows, and exits non-zero if any
check fails. With no checks registered (Phase 0) it is a green no-op and needs no DB.

Run: ``python -m backend.app.audits.runner --db <db_path>``  (or ``bih-audit --db ...``).
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

from . import AuditContext, AuditResult, CHECK_MARKER, CHECK_NAME, checks
from .baselines import BaselineStore, default_baseline_dir
from ..db.connection import get_connection


def discover_checks() -> list:
    """Return all decorated check callables found under ``audits.checks``."""
    found = []
    for modinfo in pkgutil.iter_modules(checks.__path__, checks.__name__ + "."):
        module = importlib.import_module(modinfo.name)
        for attr in vars(module).values():
            if callable(attr) and getattr(attr, CHECK_MARKER, False):
                found.append(attr)
    # Stable, deterministic order by declared check name (required so CI logs and the
    # order baseline snapshots are taken are reproducible).
    found.sort(key=lambda fn: getattr(fn, CHECK_NAME, fn.__name__))
    return found


def run_audits(db_path: str | None = None, baseline_dir: str | None = None,
               rebaseline: list[str] | None = None) -> list[AuditResult]:
    """Run all discovered checks. Opens ``db_path`` only if there is at least one check.

    ``rebaseline`` names cross-run baselines to DISCARD first (the ``--rebaseline`` escape hatch,
    review finding BASE-02): the named check then re-establishes its baseline from current state —
    an explicit, targeted operator action for a baseline verified stale out-of-band (e.g. recorded
    before a schema migration rewrote an audited table). Never use it to silence an unexplained
    failure — the discarded evidence does not come back."""
    check_fns = discover_checks()
    if not check_fns:
        return []

    if db_path is None:
        raise SystemExit("audit: checks are registered but no --db was provided")

    bdir = Path(baseline_dir) if baseline_dir else default_baseline_dir(db_path)
    store = BaselineStore(bdir)
    for name in rebaseline or []:
        store.discard(name)

    ctx = AuditContext(
        conn=get_connection(db_path, create_parents=False),
        db_path=Path(db_path),
        baselines=store,
        rebaselined=frozenset(rebaseline or []),  # let checks distinguish an explicit re-baseline (P27)
    )
    results: list[AuditResult] = []
    try:
        for fn in check_fns:
            name = getattr(fn, CHECK_NAME, fn.__name__)
            try:
                result = fn(ctx)
            except Exception as exc:  # a crashing check is itself a failure
                result = AuditResult(name=name, passed=False, detail=f"check raised: {exc!r}")
            results.append(result)
    finally:
        ctx.conn.close()
    return results


def _print_report(results: list[AuditResult]) -> bool:
    """Print results; return True if all passed."""
    if not results:
        print("audit: 0 checks registered - no-op OK")
        return True

    all_passed = True
    for r in results:
        print(f"[{r.status}] {r.name}" + (f" - {r.detail}" if r.detail else ""))
        if not r.passed:
            all_passed = False
            for row in r.offending[:20]:
                print(f"    offending: {row}")
            extra = len(r.offending) - 20
            if extra > 0:
                print(f"    ... and {extra} more")

    passed = sum(1 for r in results if r.passed)
    print(f"audit: {passed}/{len(results)} checks passed")
    return all_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run invariant audits against a case DB.")
    parser.add_argument("--db", default=None, help="path to the case.db to audit")
    parser.add_argument(
        "--baseline-dir",
        default=None,
        help="dir for cross-run audit baselines (default: <db parent>/.audit_baselines)",
    )
    parser.add_argument(
        "--rebaseline",
        action="append",
        default=None,
        metavar="CHECK",
        help="discard the named cross-run baseline before running so the check re-establishes it "
             "(explicit operator re-baseline after verifying a stale baseline, e.g. one recorded "
             "before a schema migration; repeatable)",
    )
    args = parser.parse_args(argv)

    for name in args.rebaseline or []:
        print(f"audit: discarding baseline {name!r} for explicit re-baseline (operator request)")

    results = run_audits(args.db, args.baseline_dir, rebaseline=args.rebaseline)
    ok = _print_report(results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
