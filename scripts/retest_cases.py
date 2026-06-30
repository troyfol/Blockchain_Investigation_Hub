"""`make retest` — re-run the 3 verification cases end-to-end into the APP-DATA cases folder.

For each address, in order, into ``%APPDATA%\\BlockchainInvestigationHub\\cases\\<case>\\`` (i.e.
``user_data_dir()/cases/<case>``):

  1. create a FRESH case (the folder is wiped first so a re-run is clean);
  2. ingest on-chain FACTS (Etherscan for EVM — needs the keyring key, fails loudly if absent;
     Esplora for BTC);
  3. run valuation SYNCHRONOUSLY to completion (so the report is fully valued — not the async-timing
     0/1206 a freshly-kicked background pass would show);
  4. run "Check intel" (the free OFAC SDN sanctions + GraphSense attribution pillars);
  5. generate the immutable report (HTML + PDF) into ``<case>/reports/``.

Then prints a per-case summary: sanctioned count + the actual sanctioned addresses, attribution count,
valuation M of N, and the report paths.

Expected: test_tornado -> the Tornado anchor sanctioned; test_vitalik -> its Tornado counterparty
sanctioned; test_colonial -> clean (0); all fully valued.

This is a LIVE run (real Etherscan/Esplora/DeFiLlama calls) — offline mode must be off.
"""

from __future__ import annotations

import shutil
import sys
import time
import traceback
from pathlib import Path

# Allow `python scripts/retest_cases.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.app_paths import user_data_dir
from backend.app.config import get_settings
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.services.intel import check_intel
from backend.app.services.reporting import _valuation_honesty, generate_report
from backend.app.services.valuation import value_movements

# The three verification addresses, in order. depth -> bounds mirrors frontend ingest.ts::depthBounds.
CASES = [
    {"name": "test_colonial", "address": "bc1qq2euq8pw950klpjcawuy4uj39ym43hs6cfsegq",
     "chain": "bitcoin", "depth": "standard"},
    {"name": "test_tornado", "address": "0x722122dF12D4e14e13Ac3b6895a86e84145b6967",
     "chain": "ethereum", "depth": "standard"},
    {"name": "test_vitalik", "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
     "chain": "ethereum", "depth": "shallow"},
]


def _depth_bounds(depth: str, evm: bool) -> dict:
    """Mirror frontend ingest.ts::depthBounds: a first ingest is BOUNDED (a truthful `partial` beats an
    unbounded pull). top_n_counterparties is an EVM-only (Etherscan) bound."""
    pages = 1 if depth == "shallow" else 10 if depth == "deep" else 3
    bounds: dict = {"max_pages": pages}
    if evm and depth != "deep":
        bounds["top_n_counterparties"] = 25 if depth == "shallow" else 50
    return bounds


def _build_connector(chain: str, settings):
    """The fact connector for this chain. EVM uses the keyring Etherscan key — FAIL LOUDLY if absent."""
    if _is_evm_chain(chain):
        from backend.app.connectors.etherscan import EtherscanConnector
        from backend.app.secrets import get_secret

        key = get_secret("etherscan")
        if not key:
            raise SystemExit(
                "ERROR: no Etherscan API key in the OS keyring — EVM ingest (test_tornado / test_vitalik) "
                "cannot run. Set a free key first:\n"
                "    python scripts/set_key.py            # paste your Etherscan key\n"
                "(the key is write-only to the OS keyring; it is never logged or returned).")
        return EtherscanConnector(api_key=key, settings=settings)
    from backend.app.connectors.esplora import EsploraConnector
    return EsploraConnector(settings=settings)


def _is_evm_chain(chain: str) -> bool:
    return chain.lower() != "bitcoin"


def _value_to_completion(conn, settings, *, label="", max_passes=20,
                         base_cooldown=180.0, max_cooldown=420.0) -> dict:
    """Value every PRICEABLE movement to completion, RIDING OUT DeFiLlama's free-tier rate limit.

    DeFiLlama's free pricing API (one ``/prices/historical`` call per distinct block timestamp) rate-limits
    (HTTP 429) under the volume a busy address produces (hundreds–thousands of movements). A single pass
    stops early once throttled — which is exactly how a report ends up showing a misleading 0/N even though
    the movements ARE priceable native ETH. So each pass values a CHUNK (until the burst budget is spent and
    429 hits), then we COOL DOWN long enough for the per-minute window to refill and retry the remaining
    unvalued. Two rules make this converge instead of thrash:

      * **Fail fast while throttled** (``max_retries=2``, ``max_consecutive_errors=3``): once 429 starts, a
        pass aborts after a few calls — repeatedly hammering a throttled endpoint only PROLONGS the limit.
      * **Escalating cooldown**: a productive pass resets the wait to ``base_cooldown``; consecutive
        zero-progress (still-throttled) passes back off ×1.5 up to ``max_cooldown`` so the window fully
        recovers (empirically a refilled window prices ~1.5k movements per pass).

    Unpriceable movements (e.g. spam ERC-20s with no DeFiLlama price) stay honest gaps (no row, never a
    fabricated zero): the loop stops as soon as a pass makes NO progress AND wasn't throttled. Bounded by
    ``max_passes`` so a sustained outage reports an honest partial M/N rather than hanging.
    """
    from backend.app.connectors.base import RateLimiter
    from backend.app.connectors.defillama import DeFiLlamaConnector

    cooldown = base_cooldown
    for i in range(max_passes):
        before = _valuation_honesty(conn)["valued"]
        connector = DeFiLlamaConnector(settings=settings, rate_limiter=RateLimiter(2.0),
                                       max_retries=2, backoff_cap=8.0)
        try:
            res = value_movements(conn, connector, limit=None, max_consecutive_errors=3)
        finally:
            connector.close()
        cov = _valuation_honesty(conn)
        progressed = cov["valued"] > before
        throttled = bool(res.get("price_source_unavailable") or res.get("errors"))
        if cov["missing"] == 0:
            return cov                               # everything priceable is priced
        if not progressed and not throttled:
            return cov                               # remaining are genuinely unpriceable (honest gaps)
        if not throttled:
            cooldown = base_cooldown                 # source healthy — loop straight on to value the rest
            continue
        cooldown = base_cooldown if progressed else min(max_cooldown, cooldown * 1.5)
        if i < max_passes - 1:
            print(f"    [{label}] DeFiLlama throttled (429) — {cov['valued']}/{cov['movements']} priced, "
                  f"{cov['missing']} to go; cooling down {cooldown:.0f}s then retrying...", flush=True)
            time.sleep(cooldown)
    return _valuation_honesty(conn)


def _sanctioned_rows(conn) -> list[tuple[str, str]]:
    return [(r["address"], r["source"]) for r in conn.execute(
        "SELECT DISTINCT a.address, r.source FROM risk_assessment r JOIN address a ON a.id=r.address_id "
        "WHERE r.category='sanctioned' ORDER BY a.address").fetchall()]


def _run_case(case: dict, settings) -> dict:
    name, address, chain, depth = case["name"], case["address"], case["chain"], case["depth"]
    evm = _is_evm_chain(chain)
    case_dir = user_data_dir() / "cases" / name
    if case_dir.exists():
        shutil.rmtree(case_dir)                      # FRESH case on every re-run
    case_db = case_dir / "case.db"

    print(f"\n=== {name} ({chain}, depth={depth}) — {address} ===")
    apply_migrations(case_db)
    conn = get_connection(case_db)
    try:
        repo.init_case(conn, title=f"Verification — {name}")

        # (2) ingest FACTS
        from backend.app.services.orchestrator import Orchestrator
        connector = _build_connector(chain, settings)
        try:
            Orchestrator([connector]).get_transactions(conn, chain, address, _depth_bounds(depth, evm))
        finally:
            connector.close()
        n_addr = conn.execute("SELECT COUNT(*) FROM address").fetchone()[0]
        n_tx = conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0]
        print(f"  ingested: {n_addr} addresses, {n_tx} transactions")

        # (3) valuation -> completion (synchronous; the report is fully valued)
        val = _value_to_completion(conn, settings, label=name)
        print(f"  valuation: {val['valued']} of {val['movements']} movement(s) priced "
              f"({val['missing']} honest no-price gap(s))")

        # (4) check intel (OFAC SDN sanctions + GraphSense attribution)
        intel = check_intel(conn)
        attribution_count = conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0]
        sanctioned = _sanctioned_rows(conn)
        print(f"  intel: {len(sanctioned)} sanctioned, {attribution_count} attribution claim(s) "
              f"(sources: {', '.join(intel.get('sources', [])) or 'none'})")

        # (5) report (HTML + PDF)
        rep = generate_report(conn, case_dir=case_dir, title=f"Verification report — {name}")
        return {"name": name, "address": address, "chain": chain,
                "addresses": n_addr, "transactions": n_tx,
                "valued": val["valued"], "movements": val["movements"], "missing": val["missing"],
                "sanctioned": sanctioned, "attributions": attribution_count,
                "html": rep["html_path"], "pdf": rep["pdf_path"], "engine": rep["engine"],
                "content_hash": rep["content_hash"]}
    finally:
        conn.close()


def main() -> int:
    settings = get_settings()
    from backend.app.services.settings_store import is_offline
    if is_offline():
        print("ERROR: offline mode is ON — turn it off to run a live re-test (it ingests real on-chain "
              "data). Settings -> offline, or settings_store.set_offline(False).", file=sys.stderr)
        return 2

    print(f"Re-running {len(CASES)} verification case(s) into {user_data_dir() / 'cases'}")
    results: list[dict] = []
    for case in CASES:
        try:
            results.append(_run_case(case, settings))
        except SystemExit:
            raise                                    # a missing key is fatal — surface it loudly
        except Exception as exc:  # one case failing must not hide the others' summaries
            print(f"  FAILED: {exc!r}")
            traceback.print_exc()
            results.append({"name": case["name"], "address": case["address"], "error": str(exc)})

    # --- per-case summary ---
    print("\n" + "=" * 78)
    print("RETEST SUMMARY")
    print("=" * 78)
    for r in results:
        if r.get("error"):
            print(f"\n{r['name']}: ERROR — {r['error']}")
            continue
        print(f"\n{r['name']}  ({r['chain']})  {r['address']}")
        print(f"  ingested        : {r['addresses']} addresses, {r['transactions']} txs")
        print(f"  valuation       : {r['valued']} of {r['movements']} priced "
              f"({r['missing']} no-price gap(s))")
        print(f"  sanctioned      : {len(r['sanctioned'])}")
        for addr, src in r["sanctioned"]:
            print(f"      - {addr}  [{src}]")
        print(f"  attributions    : {r['attributions']}")
        print(f"  report HTML     : {r['html']}")
        print(f"  report PDF      : {r['pdf'] if r['pdf'] else '(skipped — no browser engine; HTML is complete)'}")
        print(f"  content_hash    : {r['content_hash']}")

    # exit non-zero if any case errored (so `make retest` fails loudly in CI/manual runs)
    return 1 if any(r.get("error") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
