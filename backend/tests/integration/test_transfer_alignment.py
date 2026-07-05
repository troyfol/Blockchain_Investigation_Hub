"""FN-24 (P19): cross-source transfer alignment, end-to-end through the REAL Arkham CSV importer.

`test_cross_source_reconcile.py` locks the occurrence-dedup MACHINERY via a hand-built shim; this file
proves the same guarantees hold through the REAL `ArkhamImporter` CSV -> adapter -> reconcile -> DB path
reconciling with a chain-exact Etherscan movement, and closes the FN-24 gap: an Arkham DISPLAY amount
truncated below the asset's decimals is FLAGGED (not silently authoritative) and — being a distinct
content key — is kept side-by-side rather than collapsed into the exact figure (Invariants #4/#6/#7).
"""

from __future__ import annotations

import csv

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.imports.arkham import ArkhamImporter
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.normalization.etherscan_adapter import ParsedTransaction, ParsedTransfer
from backend.app.normalization.reconcile import assign_occurrences
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

# The real Arkham transfer-log export header (mirrors test_arkham_parser._HEADER).
_HEADER = ["transactionHash", "fromAddress", "fromLabel", "fromIsContract", "toAddress", "toLabel",
           "toIsContract", "tokenAddress", "type", "blockTimestamp", "blockNumber", "blockHash",
           "tokenName", "tokenSymbol", "tokenDecimals", "unitValue", "tokenId", "historicalUSD", "chain"]

USDC = "0x" + "a" * 40
FRM = "0x" + "1" * 40
TO = "0x" + "2" * 40
TX = "0x" + "e" * 64


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Alignment")
    yield conn, db
    conn.close()


def _write_arkham(path, **row):
    base = {k: "" for k in _HEADER}
    base.update({"transactionHash": TX, "chain": "ethereum", "blockNumber": "1",
                 "fromAddress": FRM, "toAddress": TO})
    base.update(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_HEADER)
        w.writeheader()
        w.writerow(base)
    return path


def _ingest_etherscan_exact(conn, *, amount):
    """Ingest the SAME movement as Etherscan would: a chain-exact erc20 transfer (log-order position)."""
    pt = ParsedTransaction(
        transaction=Transaction(chain="ethereum", tx_hash=TX, block_height=1, finality_status="provisional"),
        transfers=[ParsedTransfer(
            chain="ethereum", from_address=FRM, to_address=TO,
            asset=Asset(chain="ethereum", contract_address=USDC, symbol="USDC", decimals=6),
            amount=amount, transfer_type="erc20", position=0,
            from_address_display=FRM, to_address_display=TO)])
    assign_occurrences([pt])
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": FRM, "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        tx_id = repo.upsert_transaction(c, pt.transaction, sqid)
        for tr in pt.transfers:
            fid = repo.upsert_address(c, Address(chain="ethereum", address_display=tr.from_address), sqid)
            tid = repo.upsert_address(c, Address(chain="ethereum", address_display=tr.to_address), sqid)
            aid = repo.upsert_asset(c, tr.asset, sqid)
            repo.upsert_transfer(c, Transfer(
                transaction_id=tx_id, chain="ethereum", from_address_id=fid, to_address_id=tid,
                asset_id=aid, amount=tr.amount, transfer_type=tr.transfer_type,
                position=tr.position, occurrence=tr.occurrence), sqid)

    write_with_provenance(conn, sq, w)


def test_arkham_etherscan_same_tx_dedups(case, tmp_path):
    conn, db = case
    # Etherscan ingests the chain-exact movement (10.5 USDC = 10_500_000 base units) FIRST.
    _ingest_etherscan_exact(conn, amount="10500000")
    # Arkham's export of the SAME movement — full 6-dp display => the SAME base-unit amount, at a different
    # (row-order) position. Real importer path: CSV -> adapter -> reconcile -> DB.
    _write_arkham(tmp_path / "ark.csv", tokenAddress=USDC, tokenSymbol="USDC", tokenDecimals="6",
                  unitValue="10.500000")
    res = ArkhamImporter().get_transactions(conn, tmp_path / "ark.csv")

    assert res["transfers"] == 1
    # One fact, not double-counted (position differs, content matches); the FIRST writer (etherscan) wins
    # the surviving row's provenance, and BOTH source_queries are recorded (Invariants #3/#4/#7).
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 2
    winner = conn.execute(
        "SELECT sq.connector FROM transfer t JOIN source_query sq ON sq.id=t.source_query_id").fetchone()
    assert winner["connector"] == "etherscan"
    assert res["truncation_risk"] == 0                     # full-precision display => no risk
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_disagreeing_amount_stays_side_by_side_through_real_importer(case, tmp_path):
    conn, db = case
    # Etherscan's exact 10.500000 USDC vs an Arkham row that genuinely DISAGREES (10.4 USDC) — different
    # content -> BOTH kept, never silently collapsed (Invariant #4), even via the real CSV path.
    _ingest_etherscan_exact(conn, amount="10500000")
    _write_arkham(tmp_path / "ark.csv", tokenAddress=USDC, tokenSymbol="USDC", tokenDecimals="6",
                  unitValue="10.400000")
    ArkhamImporter().get_transactions(conn, tmp_path / "ark.csv")
    amounts = sorted(r[0] for r in conn.execute("SELECT amount FROM transfer").fetchall())
    assert amounts == ["10400000", "10500000"]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_truncation_flagged(case, tmp_path):
    conn, db = case
    # An 18-decimal asset whose Arkham DISPLAY shows only 4 dp: the low-order 14 base digits are
    # display-rounded. The row is still RECORDED (honest — the movement happened) but the import FLAGS the
    # truncation risk so the figure is never taken as silently authoritative (acceptance #3).
    _write_arkham(tmp_path / "trunc.csv", tokenAddress="", tokenSymbol="ETH", tokenDecimals="18",
                  unitValue="1.2346")
    res = ArkhamImporter().get_transactions(conn, tmp_path / "trunc.csv")

    assert res["transfers"] == 1 and res["truncation_risk"] == 1
    row = conn.execute("SELECT amount FROM transfer").fetchone()
    assert row["amount"] == "1234600000000000000"          # recorded at display precision, not dropped
    assert all(r.passed for r in run_audits(db_path=str(db)))

    # Re-ingesting the same export is idempotent (Invariant #7) — no duplicate row, flag stable (#4).
    res2 = ArkhamImporter().get_transactions(conn, tmp_path / "trunc.csv")
    assert res2["truncation_risk"] == 1
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))
