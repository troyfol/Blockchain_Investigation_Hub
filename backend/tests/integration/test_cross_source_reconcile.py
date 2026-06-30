"""Cross-source transfer reconciliation (docs/findings/arkham_export_reconciliation.md decision (c)).

The same on-chain movement ingested from two sources (Etherscan log-order vs Arkham/Bitquery row-order)
must dedup to ONE transfer row — never double-counted — because a transfer is a FACT keyed on its CONTENT
(+occurrence), not its source-dependent `position`. Genuinely DISAGREEING facts stay side-by-side, and
legitimately-repeated identical movements are kept distinct (Invariants #4/#7).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.normalization.etherscan_adapter import ParsedTransaction, ParsedTransfer
from backend.app.normalization.reconcile import assign_occurrences
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
D = "0x" + "d" * 40
USDT = "0x" + "1" * 40
USDC = "0x" + "2" * 40
TX = "0x" + "e" * 64


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Reconcile")
    yield conn, db
    conn.close()


def _erc20(frm, to, amount, contract, position):
    return ParsedTransfer(chain="ethereum", from_address=frm, to_address=to,
                          asset=Asset(chain="ethereum", contract_address=contract, symbol="T", decimals=6),
                          amount=amount, transfer_type="erc20", position=position)


def _ingest(conn, source, transfers):
    """Simulate a source ingesting `transfers` (one tx) — exactly what a connector does: assign the
    content+occurrence dedup ordinal, then upsert through the canonical path."""
    pt = ParsedTransaction(transaction=Transaction(chain="ethereum", tx_hash=TX, block_height=1,
                                                   finality_status="provisional"), transfers=list(transfers))
    assign_occurrences([pt])
    sq = SourceQuery(connector=source, capability="get_transactions", endpoint="x",
                     params={"address": "p", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        tx_id = repo.upsert_transaction(c, pt.transaction, sqid)
        for tr in pt.transfers:
            fid = repo.upsert_address(c, Address(chain="ethereum", address_display=tr.from_address), sqid) if tr.from_address else None
            tid = repo.upsert_address(c, Address(chain="ethereum", address_display=tr.to_address), sqid) if tr.to_address else None
            aid = repo.upsert_asset(c, tr.asset, sqid)
            repo.upsert_transfer(c, Transfer(
                transaction_id=tx_id, chain="ethereum", from_address_id=fid, to_address_id=tid,
                asset_id=aid, amount=tr.amount, transfer_type=tr.transfer_type,
                position=tr.position, occurrence=tr.occurrence), sqid)

    write_with_provenance(conn, sq, w)


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0]


def test_same_movement_two_sources_different_positions_dedups(case):
    conn, db = case
    # Source A (log-order): the movement at position 0. Source B (row-order): SAME movement at position 7.
    _ingest(conn, "etherscan", [_erc20(A, B, "100", USDT, position=0)])
    _ingest(conn, "arkham-import", [_erc20(A, B, "100", USDT, position=7)])
    assert _count(conn) == 1  # one fact, not double-counted (position differs, content matches)
    # First source's provenance WINS the surviving row (ON CONFLICT DO NOTHING); the second source's
    # source_query is still persisted (Invariants #3 — both ingests recorded, the fact attributes to A).
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 2
    winner = conn.execute(
        "SELECT sq.connector FROM transfer t JOIN source_query sq ON sq.id=t.source_query_id").fetchone()
    assert winner["connector"] == "etherscan"  # the first writer, not arkham-import
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_multi_transfer_tx_ingested_in_different_order_dedups(case):
    conn, db = case
    m1, m2 = _erc20(A, B, "100", USDT, 0), _erc20(C, D, "50", USDC, 1)
    _ingest(conn, "etherscan", [m1, m2])                 # log order [M1, M2]
    _ingest(conn, "arkham-import",                        # reversed row order [M2, M1]
            [_erc20(C, D, "50", USDC, 0), _erc20(A, B, "100", USDT, 1)])
    assert _count(conn) == 2  # M1 + M2, NOT 4 — order/position differences don't create duplicates
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_disagreeing_amount_kept_side_by_side(case):
    conn, db = case
    # Two sources disagree on the SAME movement's amount -> different content -> BOTH kept (surfaced).
    _ingest(conn, "etherscan", [_erc20(A, B, "100", USDT, 0)])
    _ingest(conn, "arkham-import", [_erc20(A, B, "101", USDT, 0)])
    amounts = sorted(r[0] for r in conn.execute("SELECT amount FROM transfer").fetchall())
    assert amounts == ["100", "101"]  # disagreement preserved, never silently collapsed (Inv #4)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_cross_source_occurrence_count_reconciles_to_higher(case):
    conn, db = case
    # Source A reports the movement ONCE; source B reports the SAME identical movement TWICE. A's occ0
    # dedups against B's occ0; B's occ1 is a genuine second movement -> reconcile to the higher count (2).
    _ingest(conn, "etherscan", [_erc20(A, B, "100", USDT, 0)])
    _ingest(conn, "arkham-import", [_erc20(A, B, "100", USDT, 0), _erc20(A, B, "100", USDT, 1)])
    assert _count(conn) == 2
    assert sorted(r[0] for r in conn.execute("SELECT occurrence FROM transfer").fetchall()) == [0, 1]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_legitimately_identical_movements_kept_and_idempotent(case):
    conn, db = case
    # A tx that really emits the same Transfer twice (A->B 100 USDT) -> two rows (occurrence 0 and 1).
    dupes = [_erc20(A, B, "100", USDT, 0), _erc20(A, B, "100", USDT, 1)]
    _ingest(conn, "etherscan", dupes)
    assert _count(conn) == 2
    occ = sorted(r[0] for r in conn.execute("SELECT occurrence FROM transfer").fetchall())
    assert occ == [0, 1]
    # Re-ingesting the same two identical movements is idempotent (occurrences match) — still 2, not 4.
    _ingest(conn, "etherscan", [_erc20(A, B, "100", USDT, 0), _erc20(A, B, "100", USDT, 1)])
    assert _count(conn) == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_migration_0007_backfills_occurrence_on_populated_db(tmp_path):
    """Forward-only safety: applying the REAL 0007 SQL to a populated pre-0007 table (two identical-
    content rows that coexisted via distinct positions) backfills distinct occurrences so the new
    content-based UNIQUE INDEX rebuilds cleanly — no data loss, no index conflict."""
    sql = (Path(__file__).resolve().parents[2] / "app" / "migrations"
           / "0007_transfer_cross_source_reconciliation.sql").read_text()
    db = tmp_path / "pre0007.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE transfer (id TEXT PRIMARY KEY, transaction_id TEXT, chain TEXT, "
        "from_address_id TEXT, to_address_id TEXT, asset_id TEXT, amount TEXT, transfer_type TEXT, "
        "position INTEGER, source_query_id TEXT);"
        "CREATE UNIQUE INDEX ux_transfer ON transfer(transaction_id, transfer_type, position);"
        "INSERT INTO transfer VALUES ('t1','tx','ethereum','A','B','USDT','100','erc20',0,'sq');"
        "INSERT INTO transfer VALUES ('t2','tx','ethereum','A','B','USDT','100','erc20',1,'sq');"  # identical content
        # distinct position under the OLD key (positions must be unique within tx/type pre-0007).
        "INSERT INTO transfer VALUES ('t3','tx','ethereum','C','D','USDC','50','erc20',2,'sq');")
    c.executescript(sql)  # apply the real migration (ALTER + ROW_NUMBER backfill + DROP/CREATE index)

    occ = dict(c.execute("SELECT id, occurrence FROM transfer").fetchall())
    assert occ == {"t1": 0, "t2": 1, "t3": 0}  # identical t1/t2 got distinct occurrences; t3 stands alone
    # The new content+occurrence unique index is live and rejects a duplicate-content+occurrence insert.
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO transfer VALUES ('t4','tx','ethereum','A','B','USDT','100','erc20',9,'sq',0)")
    c.close()
