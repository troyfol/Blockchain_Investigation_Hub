"""P10 — build the v1.0.0 SHOWCASE sample, end-to-end, into the app-data cases folder + the repo examples/.

Runs the full BIH pipeline on the Tornado Cash anchor and ships the curated artifacts the release embeds:

  1. fresh case ``Sample_Tornado_Cash``;
  2. ingest on-chain FACTS (Etherscan — needs the keyring key);
  3. value every priceable movement to COMPLETION (rides out DeFiLlama's free-tier 429s);
  4. check intel (OFAC SDN sanctions + GraphSense attribution);
  5. clustering (Victor EVM deposit-reuse) + the Leiden community overlay (computed at view time);
  6. a curated CURRENT-VIEW report (HTML + PDF) labelled ``Sample_Tornado_Cash``;
  7. a portable, verified ``.casefile``.

Then copies report.html / report.pdf / Sample_Tornado_Cash.casefile into ``examples/Sample_Tornado_Cash/``
and writes the exact curated view spec to the scratchpad so ``scripts/build_exhibit.py`` can render the
matching SVG + PNG hero. Prints every path (so the in-app graph can be screenshotted too).

LIVE run (real Etherscan/DeFiLlama). Offline must be off. ``--swap colonial|hydra`` builds an alternate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.app_paths import user_data_dir
from backend.app.config import get_settings
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.services.clustering import community as community_svc
from backend.app.services.clustering import service as clustering_service
from backend.app.services.export import export_case, verify_casefile
from backend.app.services.intel import check_intel
from backend.app.services.orchestrator import Orchestrator
from backend.app.services.reporting import generate_report
from scripts.retest_cases import _build_connector, _depth_bounds, _sanctioned_rows, _value_to_completion

NAME = "Sample_Tornado_Cash"
SHOWCASES = {
    "tornado": {"label": "Tornado Cash", "address": "0x722122dF12D4e14e13Ac3b6895a86e84145b6967",
                "chain": "ethereum", "depth": "standard", "heuristic": "evm-deposit-reuse", "community": True},
    "colonial": {"label": "Colonial Pipeline / DarkSide", "address": "bc1qq2euq8pw950klpjcawuy4uj39ym43hs6cfsegq",
                 "chain": "bitcoin", "depth": "standard", "heuristic": "btc-change", "community": False},
    "hydra": {"label": "Hydra Market", "address": "16ZSAEfYpPCj3D94fsNt2okYj9Ue8mxy6T",
              "chain": "bitcoin", "depth": "standard", "heuristic": "btc-change", "community": False},
}
# Portable temp dir shared with scripts/build_exhibit.py (the view spec hand-off). NOT a committed path.
SCRATCH = Path(tempfile.gettempdir()) / "bih_showcase"


def _seed_id(conn, address: str):
    row = conn.execute("SELECT id FROM address WHERE address=? OR address_display=?",
                       (address.lower(), address)).fetchone()
    return row[0] if row else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the v1.0.0 showcase sample.")
    ap.add_argument("--swap", choices=list(SHOWCASES), default="tornado")
    args = ap.parse_args()
    show = SHOWCASES[args.swap]
    address, chain, depth = show["address"], show["chain"], show["depth"]

    settings = get_settings()
    from backend.app.services.settings_store import is_offline
    if is_offline():
        print("ERROR: offline mode is ON — turn it off for the live showcase build.", file=sys.stderr)
        return 2

    case_dir = user_data_dir() / "cases" / NAME
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_db = case_dir / "case.db"
    t0 = time.monotonic()
    print(f"=== {NAME}: {show['label']} ({chain}, depth={depth}) — {address} ===", flush=True)
    apply_migrations(case_db)
    conn = get_connection(case_db)
    try:
        repo.init_case(conn, title=NAME)

        # (2) ingest FACTS
        connector = _build_connector(chain, settings)
        try:
            Orchestrator([connector]).get_transactions(conn, chain, address, _depth_bounds(depth, chain != "bitcoin"))
        finally:
            connector.close()
        n_addr = conn.execute("SELECT COUNT(*) FROM address").fetchone()[0]
        n_tx = conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0]
        print(f"  [1] ingested: {n_addr} addresses, {n_tx} transactions  (+{time.monotonic()-t0:.0f}s)", flush=True)

        # (3) value to completion (rides out DeFiLlama 429 — this is the slow part)
        val = _value_to_completion(conn, settings, label=NAME)
        print(f"  [2] valuation: {val['valued']}/{val['movements']} priced "
              f"({val['missing']} honest no-price gaps)  (+{time.monotonic()-t0:.0f}s)", flush=True)

        # (4) check intel (OFAC SDN + GraphSense)
        intel = check_intel(conn)
        attributions = conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0]
        sanctioned = _sanctioned_rows(conn)
        print(f"  [3] intel: {len(sanctioned)} sanctioned, {attributions} attribution(s) "
              f"(sources: {', '.join(intel.get('sources', [])) or 'none'})", flush=True)
        for addr, src in sanctioned:
            print(f"        SANCTIONED {addr} [{src}]", flush=True)

        # (5) clustering + community overlay (community is computed at view time, never persisted)
        cres = clustering_service.apply(conn, show["heuristic"], {})
        print(f"  [4] {show['heuristic']}: {cres.get('clusters', 0)} cluster(s), "
              f"{cres.get('memberships_created', 0)} memberships"
              + (f" — {cres['note']}" if cres.get("note") else ""), flush=True)
        if show["community"]:
            print(f"      Leiden community overlay available: {community_svc.leiden_available()} "
                  "(VISUAL structure only — not an ownership claim, never persisted)", flush=True)

        # (6) curated CURRENT-VIEW report (HTML + PDF), focused on the anchor + bounded + legible
        seed = _seed_id(conn, address)
        view_params = {"focus": (f"addr:{seed}" if seed else None), "hops": 2, "node_cap": 90,
                       "group_dust": True, "community_detect": show["community"]}
        rep = generate_report(conn, case_dir=case_dir, title=NAME, view_params=view_params)
        print(f"  [5] report HTML: {rep['html_path']}", flush=True)
        print(f"      report PDF : {rep['pdf_path'] or '(no engine — HTML complete)'}", flush=True)

        # (7) portable .casefile (+ self-verify)
        casefile = export_case(case_dir)
        ok = verify_casefile(casefile)["ok"]
        print(f"  [6] casefile: {casefile}  (verify: {'OK' if ok else 'FAILED'})", flush=True)
    finally:
        conn.close()

    # --- copy the shippable artifacts into the repo examples/ folder ---
    examples = ROOT / "examples" / NAME
    examples.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rep["html_path"], examples / "report.html")
    if rep["pdf_path"]:
        shutil.copy2(rep["pdf_path"], examples / "report.pdf")
    shutil.copy2(casefile, examples / f"{NAME}.casefile")

    # view spec for the exhibit renderer (scratchpad — NOT committed)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "showcase_viewspec.json").write_text(json.dumps({
        "case_db": str(case_db), "view_params": view_params, "label": show["label"],
        "examples_dir": str(examples)}, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f">> SHOWCASE BUILT in {time.monotonic()-t0:.0f}s")
    print(f">> examples/: {examples}")
    print(f">>   report.html / report.pdf / {NAME}.casefile")
    print(f">> live case (screenshot the in-app graph here): {case_db}")
    print(f">> sanctioned: {len(sanctioned)}  valued: {val['valued']}/{val['movements']}  "
          f"attributions: {attributions}")
    print(">> next: scripts/build_exhibit.py  (renders exhibit.svg + exhibit.png for the README hero)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
