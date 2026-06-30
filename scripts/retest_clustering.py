"""`make retest-clustering` (P8.8) — demonstrate the clustering heuristics end-to-end into the app-data
cases folder, with a per-case, per-heuristic cluster summary + a split/undo demonstration + a curated
CURRENT-VIEW report (legible exhibit, not the full case).

  * test_cluster_bitfinex (BTC, co-spend-rich): the Bitfinex seizure anchor
    1CGA4srJbPWhtJb7ezgY6GQf4PKhFuzD9w — ingest -> co-spend clustering + BlockSci change-address
    clustering (side-by-side) -> report.
  * test_cluster_tornado (EVM): the Tornado anchor 0x722122dF12D4e14e13Ac3b6895a86e84145b6967 — ingest ->
    deposit-reuse (Victor) + the Leiden community overlay (visual structure) -> current-view report.

Live (real Etherscan/Esplora); EVM needs the keyring Etherscan key. Offline mode must be off.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.app_paths import user_data_dir
from backend.app.config import get_settings
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.services import entities
from backend.app.services.clustering import btc_change, service
from backend.app.services.clustering import community as community_svc
from backend.app.services.intel import check_intel
from backend.app.services.orchestrator import Orchestrator
from backend.app.services.reporting import generate_report
from scripts.retest_cases import _build_connector, _depth_bounds, _is_evm_chain

CASES = [
    {"name": "test_cluster_bitfinex", "address": "1CGA4srJbPWhtJb7ezgY6GQf4PKhFuzD9w",
     "chain": "bitcoin", "depth": "standard",
     "heuristics": [("cospend", {}), ("btc-change", {})], "community": False},  # btc-change defaults to >=2 agree
    {"name": "test_cluster_tornado", "address": "0x722122dF12D4e14e13Ac3b6895a86e84145b6967",
     "chain": "ethereum", "depth": "shallow",
     "heuristics": [("evm-deposit-reuse", {})], "community": True},
]


def _addr_count(conn):
    return conn.execute("SELECT COUNT(*) FROM address").fetchone()[0]


def _run_case(case, settings):
    name, address, chain, depth = case["name"], case["address"], case["chain"], case["depth"]
    evm = _is_evm_chain(chain)
    case_dir = user_data_dir() / "cases" / name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_db = case_dir / "case.db"
    print(f"\n=== {name} ({chain}, depth={depth}) — {address} ===", flush=True)
    apply_migrations(case_db)
    conn = get_connection(case_db)
    try:
        repo.init_case(conn, title=f"Clustering demo — {name}")

        # ingest FACTS
        connector = _build_connector(chain, settings)
        try:
            Orchestrator([connector]).get_transactions(conn, chain, address, _depth_bounds(depth, evm))
        finally:
            connector.close()
        print(f"  ingested: {_addr_count(conn)} addresses", flush=True)

        # intel (gives exchange labels the deposit-reuse heuristic keys off)
        try:
            check_intel(conn, run_chainalysis=False)
        except Exception as exc:
            print(f"  (intel skipped: {exc})", flush=True)

        # apply the requested clustering heuristics (each a separate, reversible run, side-by-side)
        applied = []   # (hname, params, source_query_id) for the runs that produced memberships
        for hname, params in case["heuristics"]:
            if hname == "cospend":
                res = entities.cluster_cospend(conn)
                print(f"  cospend: {res['clusters']} cluster(s), {res['memberships_created']} memberships", flush=True)
            else:
                res = service.apply(conn, hname, params)
                print(f"  {hname}: {res.get('clusters', 0)} cluster(s), "
                      f"{res.get('memberships_created', 0)} memberships"
                      + (f" — {res['note']}" if res.get("note") else ""), flush=True)
            if res.get("source_query_id"):
                applied.append((hname, params, res["source_query_id"]))

        if case["community"]:
            print(f"  leiden community overlay available: {community_svc.leiden_available()} "
                  "(VISUAL structure only — never an ownership claim, never persisted)", flush=True)

        # REVERSIBILITY demonstration on the first run that produced memberships:
        # (1) split one address out (append-only retraction), (2) undo the whole run, (3) re-apply so the
        # report shows the clusters — proving merge/split/undo round-trip.
        if applied:
            hname, params, sqid = applied[0]
            mid = conn.execute("SELECT id FROM entity_membership WHERE source_query_id=? LIMIT 1", (sqid,)).fetchone()
            if mid:
                entities.split_address(conn, membership_id=mid[0], reason="demo-split")
                print(f"  split: 1 address split out of {hname} (retraction, append-only)", flush=True)
            undo = service.undo_run(conn, sqid)
            print(f"  undo: {hname} run undone as a unit ({undo['retracted']} retracted) — REVERSIBLE", flush=True)
            reapply = entities.cluster_cospend(conn) if hname == "cospend" else service.apply(conn, hname, params)
            print(f"  re-apply: {reapply.get('clusters', reapply.get('clusters', 0))} cluster(s) restored for the report", flush=True)

        # per-heuristic cluster summary (side-by-side: which heuristic formed each cluster + confidence)
        summary = service.cluster_summary(conn)
        for src, s in summary.items():
            sizes = ", ".join(f"size {c['size']}@conf {c['confidence_min']}–{c['confidence_max']}"
                              for c in s["clusters"][:5])
            print(f"  CLUSTERS [{src}]: {s['n_clusters']} cluster(s), {s['n_addresses']} addr — {sizes}", flush=True)
        if not summary:
            print("  CLUSTERS: none formed (honest — the data didn't satisfy any applied heuristic)", flush=True)

        # CURRENT-VIEW report (legible exhibit: focus on the anchor, bounded; community overlay for EVM)
        seed = conn.execute(
            "SELECT id FROM address WHERE address=? OR address_display=?", (address.lower(), address)).fetchone()
        view_params = {"focus": (f"addr:{seed[0]}" if seed else None), "hops": 2, "node_cap": 120,
                       "community_detect": case["community"]}
        rep = generate_report(conn, case_dir=case_dir, title=f"Clustering exhibit — {name}",
                              view_params=view_params)
        print(f"  report HTML: {rep['html_path']}", flush=True)
        print(f"  report PDF : {rep['pdf_path']}", flush=True)
        return {"name": name, "summary": summary, "html": rep["html_path"], "pdf": rep["pdf_path"]}
    finally:
        conn.close()


def main():
    settings = get_settings()
    from backend.app.services.settings_store import is_offline
    if is_offline():
        print("ERROR: offline mode is ON — turn it off for a live clustering re-test.", file=sys.stderr)
        return 2
    print(f"Re-running {len(CASES)} clustering demo case(s) into {user_data_dir() / 'cases'}", flush=True)
    for case in CASES:
        try:
            _run_case(case, settings)
        except SystemExit:
            raise
        except Exception as exc:
            import traceback
            print(f"  FAILED: {exc!r}", flush=True)
            traceback.print_exc()
    print("\nDONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
