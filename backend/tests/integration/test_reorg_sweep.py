"""Batch 3 (COR-01): the documented reorg-deletion sweep — on a COMPLETE address re-fetch, a stored
PROVISIONAL tx that is now absent (reorged/replaced) is deleted with its Family-A children, under the
fetch's source_query. Final rows stay frozen (Invariant #6). A reorged tx that already has downstream
references (valuation / trace / annotation / finding) is PRESERVED, not silently destroyed, and reported.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case


def _sq():
    return SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                       params={"address": "probe", "bounds": "default"},
                       requested_at="2026-01-01T00:00:00Z", status="ok")


def test_sweep_deletes_absent_provisional_tx(tmp_path):
    conn, db = new_case(tmp_path)
    addr = "bc1qexampleaddr"

    def write1(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid)
        # A PROVISIONAL tx paying `addr` (will be "reorged out"), and a FINAL tx paying it (must survive).
        pt = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="P" * 64, block_height=800000,
                                     confirmations=1, finality_status="provisional"), sqid, authoritative=True)
        repo.upsert_tx_output(c, TxOutput(transaction_id=pt, address_id=a, amount="10", output_index=0), sqid)
        ft = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="F" * 64, block_height=799000,
                                     confirmations=50, finality_status="final"), sqid, authoritative=True)
        repo.upsert_tx_output(c, TxOutput(transaction_id=ft, address_id=a, amount="20", output_index=0), sqid)

    write_with_provenance(conn, _sq(), write1)
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 2

    # A COMPLETE re-fetch of `addr` returns ONLY the final tx F — the provisional P is gone (reorged).
    def refetch(c, sqid):
        return repo.sweep_reorged_provisional(c, chain="bitcoin", address=addr,
                                              present_tx_hashes={"F" * 64}, source_query_id=sqid)

    _, res = write_with_provenance(conn, _sq(), refetch)
    assert res["deleted"] == ["P" * 64]
    # The provisional tx AND its output are gone; the final tx + its output survive.
    assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE tx_hash=?", ("P" * 64,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tx_output").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE tx_hash=?", ("F" * 64,)).fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


def test_sweep_cascades_derived_valuation(tmp_path):
    conn, db = new_case(tmp_path)
    addr = "bc1qexampleaddr2"

    def write1(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid)
        # A provisional tx whose output gets VALUED — a DERIVED reference (COR-01 derived-only policy).
        pt = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="V" * 64, block_height=800000,
                                     confirmations=1, finality_status="provisional"), sqid, authoritative=True)
        oid = repo.upsert_tx_output(c, TxOutput(transaction_id=pt, address_id=a, amount="30", output_index=0), sqid)
        repo.insert_valuation(c, Valuation(subject_type="tx_output", subject_id=oid, currency="USD",
                                           value="1.23", unit_price="0.041", source="defillama",
                                           price_timestamp="2026-01-01T00:00:00Z",
                                           retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, _sq(), write1)

    def refetch(c, sqid):
        return repo.sweep_reorged_provisional(c, chain="bitcoin", address=addr,
                                              present_tx_hashes=set(), source_query_id=sqid)

    _, res = write_with_provenance(conn, _sq(), refetch)
    # Derived-only policy: a valued-but-not-investigator-touched reorged tx IS deleted, and its valuation
    # is cascaded away (no dangling valuation — audits stay green).
    assert res["deleted"] == ["V" * 64]
    assert res["skipped_referenced"] == []
    assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE tx_hash=?", ("V" * 64,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0, "valuation not cascaded"
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


def test_sweep_preserves_investigator_referenced(tmp_path):
    """A reorged tx an INVESTIGATOR annotated is PRESERVED + reported — human work is never destroyed."""
    from backend.app.services.investigator import add_annotation
    conn, db = new_case(tmp_path)
    addr = "bc1qinvestigatortouched"

    def write1(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid)
        pt = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="I" * 64, block_height=800000,
                                     confirmations=1, finality_status="provisional"), sqid, authoritative=True)
        repo.upsert_tx_output(c, TxOutput(transaction_id=pt, address_id=a, amount="30", output_index=0), sqid)
        return pt

    _, pt = write_with_provenance(conn, _sq(), write1)
    add_annotation(conn, target_type="transaction", target_id=pt, content="suspicious peel")

    def refetch(c, sqid):
        return repo.sweep_reorged_provisional(c, chain="bitcoin", address=addr,
                                              present_tx_hashes=set(), source_query_id=sqid)

    _, res = write_with_provenance(conn, _sq(), refetch)
    assert res["deleted"] == []
    assert res["skipped_referenced"] == ["I" * 64], "an investigator-annotated reorged tx must be preserved"
    assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE tx_hash=?", ("I" * 64,)).fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


@respx.mock
def test_sweep_wired_into_esplora_complete_fetch(tmp_path):
    """The sweep is REACHABLE from the real ingest path: a complete Esplora re-fetch that no longer lists
    a previously-provisional tx deletes it (not dead code — cf. LOG-04)."""
    conn, db = new_case(tmp_path)
    addr = "bc1qwiredsweep"

    # Seed a provisional tx paying `addr` out-of-band.
    def seed(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid)
        pt = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="a" * 64, block_height=800000,
                                     confirmations=1, finality_status="provisional"), sqid, authoritative=True)
        repo.upsert_tx_output(c, TxOutput(transaction_id=pt, address_id=a, amount="10", output_index=0), sqid)

    write_with_provenance(conn, _sq(), seed)

    # A complete Esplora re-fetch of `addr` returns an EMPTY tx list (the tx reorged out).
    def router(request):
        path = request.url.path
        if path.endswith("/blocks/tip/height"):
            return httpx.Response(200, text="800100")
        if f"/address/{addr}/txs" in path or "/txs/chain/" in path:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"chain_stats": {}, "mempool_stats": {}})

    respx.route(host="blockstream.info").mock(side_effect=router)
    c = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                         sleep=lambda _s: None)
    try:
        c.get_transactions(conn, "bitcoin", addr)
    finally:
        c.close()
    assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE tx_hash=?", ("a" * 64,)).fetchone()[0] == 0
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()
