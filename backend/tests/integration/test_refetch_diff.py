"""Re-fetch snapshot / diff (P23/FN-13): what a re-fetch changed — new rows, finality maturation, corrections.

The diff is a read-only before/after around the ONE sanctioned mutation (a re-fetch of provisional data,
Invariants #6/#7). These tests drive the snapshot/diff functions directly with two hand-built ingests (no
network) so the finality flip, the zero-dup guarantee, and a reorg correction are all deterministic.
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.refetch_diff import capture_snapshot, compute_diff
from backend.tests.integration._helpers import new_case

FRM = "0x" + "1" * 40
TO = "0x" + "2" * 40
TO2 = "0x" + "3" * 40
TXA = "0x" + "a" * 64
TXB = "0x" + "b" * 64
ETH = Asset(chain="ethereum", contract_address=None, symbol="ETH", decimals=18)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Re-fetch diff")
    yield conn, db
    conn.close()


def _ingest(conn, *, connector, when, tx, transfers, authoritative=True):
    """Ingest one tx + its native transfers under a fresh source_query (a single 'fetch')."""
    sq = SourceQuery(connector=connector, capability="get_transactions", endpoint="txlist",
                     params={"address": FRM, "bounds": "default"}, requested_at=when, status="ok")

    def w(c, sqid):
        tx_id = repo.upsert_transaction(c, tx, sqid, authoritative=authoritative)
        for frm, to, amount, pos in transfers:
            fid = repo.upsert_address(c, Address(chain=tx.chain, address_display=frm), sqid)
            tid = repo.upsert_address(c, Address(chain=tx.chain, address_display=to), sqid)
            aid = repo.upsert_asset(c, ETH, sqid)
            repo.upsert_transfer(c, Transfer(
                transaction_id=tx_id, chain=tx.chain, from_address_id=fid, to_address_id=tid,
                asset_id=aid, amount=amount, transfer_type="native", position=pos, occurrence=0), sqid)

    write_with_provenance(conn, sq, w)


def _prov(tx_hash, *, block_height=100, confirmations=3, status="success"):
    return Transaction(chain="ethereum", tx_hash=tx_hash, block_height=block_height,
                       block_ts="2024-01-01T00:00:00Z", status=status, confirmations=confirmations,
                       finality_status="provisional")


def _final(tx_hash, *, block_height=100, confirmations=64, status="success"):
    return Transaction(chain="ethereum", tx_hash=tx_hash, block_height=block_height,
                       block_ts="2024-01-01T00:00:00Z", status=status, confirmations=confirmations,
                       finality_status="final")


def test_reports_new_rows_and_finality_flips(case):
    conn, db = case
    # Fetch 1: a provisional tx TXA with one native transfer.
    _ingest(conn, connector="etherscan", when="2026-01-01T00:00:00Z", tx=_prov(TXA),
            transfers=[(FRM, TO, "1000000000000000000", 0)])
    before = capture_snapshot(conn)

    # Fetch 2 (the re-fetch): TXA matures provisional -> final (same transfer, re-fetched = a no-op), AND a
    # brand-new confirmed tx TXB with a new transfer is discovered.
    _ingest(conn, connector="etherscan", when="2026-01-02T00:00:00Z", tx=_final(TXA),
            transfers=[(FRM, TO, "1000000000000000000", 0)])          # identical -> DO NOTHING
    _ingest(conn, connector="etherscan", when="2026-01-02T00:00:01Z", tx=_final(TXB),
            transfers=[(FRM, TO2, "2000000000000000000", 0)])

    diff = compute_diff(conn, before)
    assert diff["new_transfers"] == 1                                  # only TXB's transfer; TXA's didn't dup
    assert [f["tx_hash"] for f in diff["provisional_to_final"]] == [TXA]
    assert diff["corrected"] == []
    assert diff["summary"] == "+1 transfers, 1 provisional→final, 0 corrected"
    assert diff["changed"] is True

    # Zero-dup (Invariant #7): the identical re-fetched movement did NOT duplicate — 2 transfers total.
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_identical_refetch_shows_zero_changes(case):
    conn, db = case
    _ingest(conn, connector="etherscan", when="2026-01-01T00:00:00Z", tx=_prov(TXA),
            transfers=[(FRM, TO, "1000000000000000000", 0)])
    before = capture_snapshot(conn)
    # Re-fetch the SAME provisional data — nothing matured, nothing new (Invariant #7).
    _ingest(conn, connector="etherscan", when="2026-01-02T00:00:00Z", tx=_prov(TXA),
            transfers=[(FRM, TO, "1000000000000000000", 0)])

    diff = compute_diff(conn, before)
    assert diff["new_transfers"] == 0 and diff["provisional_to_final"] == [] and diff["corrected"] == []
    assert diff["changed"] is False
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 1


def test_corrected_provisional_fact_is_surfaced(case):
    conn, db = case
    # Fetch 1: provisional tx at block 100.
    _ingest(conn, connector="etherscan", when="2026-01-01T00:00:00Z", tx=_prov(TXA, block_height=100),
            transfers=[(FRM, TO, "1000000000000000000", 0)])
    before = capture_snapshot(conn)
    # Fetch 2: a reorg moves it to block 101, still provisional (authoritative chain source replaces it).
    _ingest(conn, connector="etherscan", when="2026-01-02T00:00:00Z", tx=_prov(TXA, block_height=101),
            transfers=[(FRM, TO, "1000000000000000000", 0)])

    diff = compute_diff(conn, before)
    assert diff["provisional_to_final"] == []                          # did not mature
    assert len(diff["corrected"]) == 1 and diff["corrected"][0]["tx_hash"] == TXA
    assert "100|" in diff["corrected"][0]["before"] and "101|" in diff["corrected"][0]["after"]
    assert diff["changed"] is True
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_confirmations_tick_on_provisional_is_not_a_correction(case):
    conn, db = case
    # A provisional tx re-fetched with MORE confirmations (3 -> 5) but still below the finality threshold is
    # normal maturation, NOT a correction — the signature excludes confirmations, so it is not flagged.
    _ingest(conn, connector="etherscan", when="2026-01-01T00:00:00Z", tx=_prov(TXA, confirmations=3),
            transfers=[(FRM, TO, "1000000000000000000", 0)])
    before = capture_snapshot(conn)
    _ingest(conn, connector="etherscan", when="2026-01-02T00:00:00Z", tx=_prov(TXA, confirmations=5),
            transfers=[(FRM, TO, "1000000000000000000", 0)])

    diff = compute_diff(conn, before)
    assert diff["corrected"] == [] and diff["provisional_to_final"] == [] and diff["changed"] is False


def test_diff_is_read_only(case):
    conn, db = case
    _ingest(conn, connector="etherscan", when="2026-01-01T00:00:00Z", tx=_prov(TXA),
            transfers=[(FRM, TO, "1000000000000000000", 0)])
    before = capture_snapshot(conn)
    _ingest(conn, connector="etherscan", when="2026-01-02T00:00:00Z", tx=_final(TXA),
            transfers=[(FRM, TO, "1000000000000000000", 0)])

    counts = lambda: tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("transfer", "transaction_", "source_query"))
    pre = counts()
    capture_snapshot(conn)
    compute_diff(conn, before)
    assert counts() == pre                                             # snapshot + diff wrote nothing
