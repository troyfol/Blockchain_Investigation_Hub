"""Etherscan CSV export import — contract + adapter tests (P22/FN-25).

Etherscan's UI "Download CSV Export" (normal transactions) is a public, key-less way to get an address's
tx history. EVM native rows map to canonical `transfer` facts; the SAME movement pulled from the Etherscan
**API** must dedup (content+occurrence, Invariant #7), a reverted tx moves no value (no transfer), and a
display amount truncated below 18 dp is FLAGGED (FN-24/P19). The fixture `etherscan_export.csv` is a
hand-built faithful sample of the documented public export header (a UI export format is public/structural —
not a fabricated API cassette).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.base import ConnectorError
from backend.app.connectors.imports.etherscan_csv import EtherscanCsvImporter
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.normalization.etherscan_adapter import ParsedTransaction, ParsedTransfer
from backend.app.normalization.etherscan_csv_adapter import adapt_etherscan_csv
from backend.app.normalization.reconcile import assign_occurrences
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "imports"

# The modern Etherscan normal-tx export header (matches the fixture).
_HEADER = ["Transaction Hash", "Blockno", "UnixTimestamp", "DateTime (UTC)", "From", "To",
           "ContractAddress", "Value_IN(ETH)", "Value_OUT(ETH)", "CurrentValue @ $2500.00/Eth",
           "TxnFee(ETH)", "TxnFee(USD)", "Historical $Price/Eth", "Status", "ErrCode", "Method"]

ADDR = "0x52908400098527886E0F7030069857D2E4169EE7"      # exported address (checksummed)
ADDR_C = ADDR.lower()                                     # canonical
VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
VITALIK_C = VITALIK.lower()
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"

TX1 = "0xa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"  # OUT 0.5
TX2 = "0xb2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1"  # IN 1.25
TX3 = "0xc3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2"  # failed
TX4 = "0xd4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3"  # zero-value
TX5 = "0xe5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"  # OUT 0.099 (truncated)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Etherscan CSV")
    yield conn, db
    conn.close()


# --- contract tests over the REAL fixture export ---------------------------------------------

def test_parses_export_to_canonical(case):
    conn, db = case
    res = EtherscanCsvImporter().get_transactions(conn, FIX / "etherscan_export.csv")

    # 5 txs; 3 native transfers (TX1 out, TX2 in, TX5 out); TX3 failed + TX4 zero-value -> NO transfer.
    assert res["transactions"] == 5 and res["transfers"] == 3
    assert res["failed"] == 1 and res["skipped"] == 1
    assert res["truncation_risk"] == 1 and res["rounded_amounts"] == 0 and res["chain"] == "ethereum"

    def tx(txh):
        return conn.execute(
            """SELECT t.amount, t.transfer_type, a.symbol, a.decimals, a.contract_address,
                      fa.address AS from_addr, ta.address AS to_addr,
                      x.finality_status, x.confirmations, x.status, x.fee
               FROM transaction_ x
               LEFT JOIN transfer t ON t.transaction_id=x.id
               LEFT JOIN asset a ON a.id=t.asset_id
               LEFT JOIN address fa ON fa.id=t.from_address_id
               LEFT JOIN address ta ON ta.id=t.to_address_id
               WHERE x.tx_hash=?""", (txh,)).fetchone()

    out = tx(TX1)
    assert out["amount"] == "500000000000000000" and out["transfer_type"] == "native"
    assert out["symbol"] == "ETH" and out["decimals"] == 18 and out["contract_address"] is None
    assert out["from_addr"] == ADDR_C and out["to_addr"] == VITALIK_C
    assert out["finality_status"] == "provisional" and out["confirmations"] is None
    assert out["status"] == "success" and out["fee"] == "2100000000000000"   # 0.0021 ETH -> wei

    inc = tx(TX2)
    assert inc["amount"] == "1250000000000000000"
    assert inc["from_addr"] == VITALIK_C and inc["to_addr"] == ADDR_C

    trunc = tx(TX5)
    assert trunc["amount"] == "99000000000000000"    # 0.099 recorded at display precision, flagged not dropped

    # TX3 reverted: a transaction row (status='failed') with NO transfer — never a fabricated movement.
    failed = tx(TX3)
    assert failed["status"] == "failed" and failed["amount"] is None
    # TX4 zero-value contract call: recorded tx, NO transfer.
    zero = tx(TX4)
    assert zero["status"] == "success" and zero["amount"] is None

    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 3
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_csv_dedups_against_api_pull(case, tmp_path):
    conn, db = case
    # The Etherscan API pulls the chain-exact native move FIRST (0.5 ETH = 5e17 wei, log-order position).
    _ingest_api_native(conn, tx_hash=TX1, frm=ADDR, to=VITALIK, amount="500000000000000000")
    # The CSV export of the SAME tx — full-precision display => the SAME base-unit amount, row-order position.
    _write_etherscan(tmp_path / "one.csv", **{"Transaction Hash": TX1, "From": ADDR, "To": VITALIK,
                                              "Value_OUT(ETH)": "0.500000000000000000", "Blockno": "18000000",
                                              "UnixTimestamp": "1700000000"})
    res = EtherscanCsvImporter().get_transactions(conn, tmp_path / "one.csv")

    assert res["transfers"] == 1 and res["truncation_risk"] == 0
    # One fact, not double-counted (position differs, content matches); the FIRST writer (API) keeps the
    # surviving row's provenance, and BOTH source_queries are recorded (Invariants #3/#4/#7).
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 2
    winner = conn.execute(
        "SELECT sq.connector FROM transfer t JOIN source_query sq ON sq.id=t.source_query_id").fetchone()
    assert winner["connector"] == "etherscan-api"
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_reingest_is_idempotent(case):
    conn, db = case
    EtherscanCsvImporter().get_transactions(conn, FIX / "etherscan_export.csv")
    EtherscanCsvImporter().get_transactions(conn, FIX / "etherscan_export.csv")  # same file/order
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 3       # no dupes
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 5
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_does_not_synthesize_attribution(case):
    conn, db = case
    EtherscanCsvImporter().get_transactions(conn, FIX / "etherscan_export.csv")
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0


# --- refusals / clean errors -----------------------------------------------------------------

def test_non_evm_chain_refused(case):
    conn, _ = case
    with pytest.raises(ConnectorError) as exc:
        EtherscanCsvImporter().get_transactions(conn, FIX / "etherscan_export.csv", chain="bitcoin")
    assert "Invariant #5" in str(exc.value) and "EVM" in str(exc.value)
    # Refused BEFORE any write (guard is up front): nothing imported.
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 0


def test_missing_columns_is_clean_error(case, tmp_path):
    conn, _ = case
    bad = tmp_path / "notetherscan.csv"
    with open(bad, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["foo", "bar", "baz"])
        w.writerow(["1", "2", "3"])
    with pytest.raises(ConnectorError) as exc:
        EtherscanCsvImporter().get_transactions(conn, bad)
    assert "not an Etherscan" in str(exc.value)
    for table in ("transfer", "transaction_", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_malformed_address_is_a_clean_error(case, tmp_path):
    conn, _ = case
    _write_etherscan(tmp_path / "bad.csv", **{"Transaction Hash": TX1, "From": "0xNOTHEX", "To": VITALIK,
                                             "Value_OUT(ETH)": "1", "Blockno": "1"})
    with pytest.raises(ConnectorError) as exc:
        EtherscanCsvImporter().get_transactions(conn, tmp_path / "bad.csv")
    assert "unparseable" in str(exc.value)
    for table in ("transfer", "transaction_", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# --- pure-adapter unit tests for the mapping edge cases --------------------------------------

def _row(**kw):
    base = {k: "" for k in _HEADER}
    base.update({"Transaction Hash": "0xtx", "Blockno": "1", "UnixTimestamp": "1700000000",
                 "From": ADDR, "To": VITALIK, "Value_IN(ETH)": "0", "Value_OUT(ETH)": "0"})
    base.update(kw)
    return base


def _adapt(rows, **kw):
    return adapt_etherscan_csv(rows, fieldnames=_HEADER, **kw)


def test_adapter_out_direction_amount():
    bundles, notes = _adapt([_row(**{"Value_OUT(ETH)": "2.5"})])
    assert len(bundles) == 1 and notes["transfers"] == 1
    tr = bundles[0].transfers[0]
    assert tr.amount == "2500000000000000000" and tr.transfer_type == "native"
    assert tr.from_address == ADDR_C and tr.to_address == VITALIK_C
    assert tr.asset.contract_address is None and tr.asset.symbol == "ETH"


def test_adapter_in_direction_amount():
    bundles, notes = _adapt([_row(**{"From": VITALIK, "To": ADDR, "Value_IN(ETH)": "0.75"})])
    tr = bundles[0].transfers[0]
    assert tr.amount == "750000000000000000"
    assert tr.from_address == VITALIK_C and tr.to_address == ADDR_C


def test_adapter_zero_value_yields_no_transfer():
    bundles, notes = _adapt([_row()])  # both IN and OUT 0
    assert len(bundles) == 1 and bundles[0].transfers == [] and notes["skipped"] == 1


def test_adapter_failed_yields_no_transfer():
    bundles, notes = _adapt([_row(**{"Value_OUT(ETH)": "1", "ErrCode": "Reverted"})])
    assert bundles[0].transaction.status == "failed" and bundles[0].transfers == []
    assert notes["failed"] == 1


def test_adapter_status_error_text_is_failure():
    # ErrCode empty but Status carries the failure — still no transfer (decision (d)).
    bundles, notes = _adapt([_row(**{"Value_OUT(ETH)": "1", "Status": "Error(0)"})])
    assert bundles[0].transaction.status == "failed" and notes["failed"] == 1


def test_adapter_truncation_risk_flag():
    rows = [_row(**{"Transaction Hash": "0xshort", "Value_OUT(ETH)": "0.099"}),   # 3 dp < 18 -> flagged
            _row(**{"Transaction Hash": "0xfull", "Value_OUT(ETH)": "0.500000000000000000"})]  # 18 dp -> exact
    bundles, notes = _adapt(rows)
    amounts = {b.transaction.tx_hash: b.transfers[0].amount for b in bundles}
    assert amounts["0xshort"] == "99000000000000000" and amounts["0xfull"] == "500000000000000000"
    assert notes["truncation_risk"] == 1 and notes["rounded_amounts"] == 0


def test_adapter_self_send_is_one_transfer():
    # A self-transfer shows the same value in BOTH columns — ONE transfer, never summed/doubled.
    bundles, notes = _adapt([_row(**{"From": ADDR, "To": ADDR,
                                     "Value_IN(ETH)": "1.0", "Value_OUT(ETH)": "1.0"})])
    assert notes["transfers"] == 1
    tr = bundles[0].transfers[0]
    assert tr.amount == "1000000000000000000" and tr.from_address == ADDR_C and tr.to_address == ADDR_C


def test_adapter_classic_header_aliases():
    # The classic export used `Txhash`/`DateTime` (no space / no "(UTC)"); era-robust resolution ingests it.
    classic = ["Txhash", "Blockno", "UnixTimestamp", "DateTime", "From", "To", "ContractAddress",
               "Value_IN(ETH)", "Value_OUT(ETH)", "TxnFee(ETH)", "Status", "ErrCode"]
    row = {k: "" for k in classic}
    row.update({"Txhash": "0xclassic", "Blockno": "1", "UnixTimestamp": "1700000000",
                "From": ADDR, "To": VITALIK, "Value_OUT(ETH)": "3"})
    bundles, notes = adapt_etherscan_csv([row], fieldnames=classic)
    assert notes["transfers"] == 1 and bundles[0].transaction.tx_hash == "0xclassic"
    assert bundles[0].transfers[0].amount == "3000000000000000000"


def test_adapter_missing_required_column_short_circuits():
    bundles, notes = adapt_etherscan_csv([{"foo": "1"}], fieldnames=["foo"])
    assert bundles == [] and notes["errors"] and notes["errors"][0]["row"] == -1
    assert "not an Etherscan" in notes["errors"][0]["reason"]


def test_adapter_provisional_finality_and_ts():
    bundles, _ = _adapt([_row(**{"Value_OUT(ETH)": "1", "UnixTimestamp": "1700000000"})])
    txn = bundles[0].transaction
    assert txn.finality_status == "provisional" and txn.confirmations is None
    assert txn.block_ts == "2023-11-14T22:13:20Z"     # unix epoch -> canonical ISO


# --- helpers ---------------------------------------------------------------------------------

def _write_etherscan(path, **overrides):
    """Write a single-row modern Etherscan normal-tx export CSV (defaults + per-call overrides)."""
    base = {k: "" for k in _HEADER}
    base.update({"Transaction Hash": TX1, "Blockno": "1", "UnixTimestamp": "1700000000",
                 "From": ADDR, "To": VITALIK, "Value_IN(ETH)": "0", "Value_OUT(ETH)": "0"})
    base.update(overrides)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_HEADER)
        w.writeheader()
        w.writerow(base)
    return path


def _ingest_api_native(conn, *, tx_hash, frm, to, amount):
    """Ingest the SAME movement as the Etherscan API would: a chain-exact native transfer (position 0)."""
    pt = ParsedTransaction(
        transaction=Transaction(chain="ethereum", tx_hash=tx_hash, block_height=18000000,
                                finality_status="provisional"),
        transfers=[ParsedTransfer(
            chain="ethereum", from_address=frm.lower(), to_address=to.lower(),
            asset=Asset(chain="ethereum", contract_address=None, symbol="ETH", decimals=18),
            amount=amount, transfer_type="native", position=0,
            from_address_display=frm, to_address_display=to)])
    assign_occurrences([pt])
    sq = SourceQuery(connector="etherscan-api", capability="get_transactions", endpoint="txlist",
                     params={"address": frm, "bounds": "default"},
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
