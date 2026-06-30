"""Arkham transfer-log import — contract + adapter tests (re-scoped 2026-06-28).

Arkham's UI export is a TRANSFER LOG, not attributions (docs/findings/arkham_export_reconciliation.md).
EVM rows map to canonical `transfer` facts; **Bitcoin** rows would fabricate a UTXO input→output edge
(Invariant #5) and are hard-refused; **Tron** (account-model but unsupported) is skipped-and-reported.
Fixtures are the real exports: `arkham_txns.csv` (1 ETH row), `arkham_satoshi.csv` (16 BTC rows),
`arkham_bsc_native_multitx.csv` (16 bsc rows), `arkham_multichain_tron.csv` (bsc/eth/base/tron).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.base import ConnectorError
from backend.app.connectors.imports.arkham import ArkhamImporter
from backend.app.normalization.arkham_adapter import adapt_arkham_transfers
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "imports"
TX_94A4 = "0x94a4b009079629772dc6d40b71ab63854c7102580ff0d8d26177e6756c9af4c2"  # bsc, 3 transfers
TX_EDCE = "0xedce763ca9a3f879f8d1c33a0de99d6bf876477959eb95f2a418b9bcfc096841"  # bsc, 2 transfers
TX_BC8E = "0xbc8e8e9c8903c56b1e43ec1b967d7a4ce0192960f0927ed137f87d6d1225b2d3"  # bsc, 0.099 BNB


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Arkham")
    yield conn, db
    conn.close()


# --- contract tests over the REAL exports ----------------------------------------------------

def test_real_evm_export_maps_to_transfer(case):
    conn, db = case
    res = ArkhamImporter().get_transactions(conn, FIX / "arkham_txns.csv")
    assert res["transactions"] == 1 and res["transfers"] == 1

    row = conn.execute(
        """SELECT t.amount, t.transfer_type, a.symbol, a.decimals, a.contract_address,
                  ta.address AS to_addr, x.finality_status, x.confirmations
           FROM transfer t JOIN asset a ON a.id=t.asset_id
           JOIN transaction_ x ON x.id=t.transaction_id
           LEFT JOIN address ta ON ta.id=t.to_address_id""").fetchone()
    assert row["amount"] == "32000000" and row["transfer_type"] == "erc20" and row["decimals"] == 6
    assert row["contract_address"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert row["to_addr"] == "0x52908400098527886e0f7030069857d2e4169ee7"
    assert row["finality_status"] == "provisional" and row["confirmations"] is None
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_bitcoin_export_is_refused_all_or_nothing(case):
    conn, db = case
    # The real satoshi export is 16 bitcoin rows; 5 of them have a comma-joined (multi-address) fromAddress.
    rows = list(csv.DictReader((FIX / "arkham_satoshi.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert len(rows) == 16
    assert sum("," in r["fromAddress"] for r in rows) == 5

    # The adapter classifies ALL 16 as UTXO and NEVER canonicalizes them (so the comma-joined input sets
    # never become addresses) — bundles empty, no parse errors.
    bundles, notes = adapt_arkham_transfers(rows)
    assert bundles == [] and len(notes["rejected_utxo"]) == 16
    assert notes["rejected_unsupported"] == [] and notes["errors"] == []

    # The importer refuses with a clean Invariant #5 error and writes NOTHING (all-or-nothing rollback).
    with pytest.raises(ConnectorError) as exc:
        ArkhamImporter().get_transactions(conn, FIX / "arkham_satoshi.csv")
    assert "Invariant #5" in str(exc.value) and "bitcoin" in str(exc.value).lower()
    for table in ("transaction_", "transfer", "asset", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_bsc_native_multitx_ingests_with_positions(case):
    conn, db = case
    res = ArkhamImporter().get_transactions(conn, FIX / "arkham_bsc_native_multitx.csv")
    assert res["transactions"] == 13 and res["transfers"] == 16   # bsc is EVM now — NOT rejected

    # Native BNB (decimals 18): amount = round(Decimal(unitValue) × 10^18). tx 0xbc8e8e… = 0.099 BNB.
    bnb = conn.execute(
        """SELECT t.amount, t.transfer_type, a.symbol, a.decimals, a.contract_address, x.chain
           FROM transfer t JOIN asset a ON a.id=t.asset_id JOIN transaction_ x ON x.id=t.transaction_id
           WHERE x.tx_hash=?""", (TX_BC8E,)).fetchone()
    assert bnb["amount"] == "99000000000000000" and bnb["transfer_type"] == "native"
    assert bnb["symbol"] == "BNB" and bnb["decimals"] == 18 and bnb["contract_address"] is None
    assert bnb["chain"] == "bsc"

    # Multi-transfer txs get dense positions within (tx, transfer_type), in CSV row order.
    def positions(txh):
        return [r[0] for r in conn.execute(
            "SELECT t.position FROM transfer t JOIN transaction_ x ON x.id=t.transaction_id "
            "WHERE x.tx_hash=? ORDER BY t.position", (txh,)).fetchall()]
    assert positions(TX_94A4) == [0, 1, 2] and positions(TX_EDCE) == [0, 1]

    # The one BSC USDT row is erc20 with 18 decimals (BSC USDT != Ethereum's 6).
    usdt = conn.execute("SELECT decimals FROM asset WHERE symbol='USDT'").fetchone()
    assert usdt["decimals"] == 18
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_bsc_reingest_is_idempotent(case):
    conn, db = case
    ArkhamImporter().get_transactions(conn, FIX / "arkham_bsc_native_multitx.csv")
    ArkhamImporter().get_transactions(conn, FIX / "arkham_bsc_native_multitx.csv")  # same file/order
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 16  # no dupes, stable positions
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_multichain_ingests_evm_skips_tron(case):
    conn, db = case
    res = ArkhamImporter().get_transactions(conn, FIX / "arkham_multichain_tron.csv")
    # bsc(2) + ethereum(4) + base(1) ingested; tron(9) skipped (unsupported, NOT a UTXO/Invariant-#5 case).
    assert res["transfers"] == 7 and res["transactions"] == 7
    assert res["unsupported_skipped"] == 9 and res["unsupported_chains"] == ["tron"]

    # Mixed decimals both present (USDC=6, BNB/PEPE=18); a base USDC transfer maps cleanly.
    base_usdc = conn.execute(
        """SELECT t.amount, a.decimals FROM transfer t JOIN asset a ON a.id=t.asset_id
           JOIN transaction_ x ON x.id=t.transaction_id WHERE x.chain='base' AND a.symbol='USDC'""").fetchone()
    assert base_usdc["amount"] == "16800000" and base_usdc["decimals"] == 6
    chains = {r[0] for r in conn.execute("SELECT DISTINCT chain FROM transfer").fetchall()}
    assert chains == {"bsc", "ethereum", "base"}                    # tron NOT written
    # Tron base58 (T…) addresses never reached canonical_address — none were created.
    assert conn.execute("SELECT COUNT(*) FROM address WHERE address LIKE 'T%'").fetchone()[0] == 0
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_does_not_synthesize_attribution(case):
    conn, db = case
    ArkhamImporter().get_transactions(conn, FIX / "arkham_txns.csv")
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0


# --- pure-adapter unit tests for the mapping edge cases --------------------------------------

def _row(**kw):
    base = {"transactionHash": "0xtx", "chain": "ethereum", "unitValue": "1", "tokenAddress": "",
            "tokenSymbol": "ETH", "tokenDecimals": "18", "blockNumber": "1", "blockTimestamp": "",
            "fromAddress": "0x" + "1" * 40, "toAddress": "0x" + "2" * 40, "type": "", "tokenId": ""}
    base.update(kw)
    return base


def test_adapter_assigns_positions_within_tx():
    usdt = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    usdc = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    rows = [_row(transactionHash="0xA", tokenAddress=usdt, tokenSymbol="USDT", tokenDecimals="6", unitValue="32"),
            _row(transactionHash="0xA", tokenAddress=usdc, tokenSymbol="USDC", tokenDecimals="6", unitValue="10")]
    bundles, notes = adapt_arkham_transfers(rows)
    assert len(bundles) == 1 and sorted(t.position for t in bundles[0].transfers) == [0, 1]


def test_adapter_native_and_rounding():
    rows = [
        _row(transactionHash="0xN", tokenAddress="", tokenSymbol="ETH", tokenDecimals="18", unitValue="1.5"),
        _row(transactionHash="0xR", tokenAddress="0x" + "a" * 40, tokenSymbol="X", tokenDecimals="6",
             unitValue="1.2345678"),  # 7 dp into a 6-dp token -> rounded
    ]
    bundles, notes = adapt_arkham_transfers(rows)
    native = next(t for b in bundles for t in b.transfers if t.transfer_type == "native")
    assert native.amount == "1500000000000000000" and native.asset.contract_address is None
    assert notes["rounded_amounts"] == 1


def test_adapter_classifies_utxo_vs_unsupported():
    btc_multi = _row(transactionHash="0xbtc1", chain="bitcoin", tokenAddress="", tokenSymbol="BTC",
                     tokenDecimals="8", unitValue="0.001", fromAddress="bc1aaa,bc1bbb", type="inflow")
    btc_single = _row(transactionHash="0xbtc2", chain="bitcoin", tokenAddress="", tokenSymbol="BTC",
                      tokenDecimals="8", unitValue="0.001", fromAddress="bc1ccc")  # single addr, still UTXO
    tron = _row(transactionHash="0xtrx", chain="tron", tokenAddress="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                tokenSymbol="USDT", tokenDecimals="6", unitValue="100", fromAddress="TJqwA7SoZnERE4zW5uDEi")
    bundles, notes = adapt_arkham_transfers([btc_multi, btc_single, tron])
    assert bundles == []
    assert len(notes["rejected_utxo"]) == 2          # both BTC rows — rejection keys off CHAIN, not commas
    assert len(notes["rejected_unsupported"]) == 1   # tron is account-model-but-unsupported, NOT utxo
    assert notes["rejected_unsupported"][0]["chain"] == "tron"


def test_adapter_alias_normalizes_chain():
    rows = [_row(transactionHash="0xz", chain="binance-smart-chain", tokenSymbol="BNB", tokenDecimals="18",
                 unitValue="1")]
    bundles, _ = adapt_arkham_transfers(rows)
    assert bundles and bundles[0].transaction.chain == "bsc"  # alias -> canonical


def test_adapter_mint_and_burn_endpoints():
    # No real row had an empty endpoint; cover synthetically — empty from = mint, empty to = burn.
    rows = [_row(transactionHash="0xmint", fromAddress="", toAddress="0x" + "2" * 40),
            _row(transactionHash="0xburn", fromAddress="0x" + "1" * 40, toAddress="")]
    bundles, _ = adapt_arkham_transfers(rows)
    by = {b.transaction.tx_hash: b.transfers[0] for b in bundles}
    assert by["0xmint"].from_address is None and by["0xmint"].to_address is not None
    assert by["0xburn"].to_address is None and by["0xburn"].from_address is not None


# --- malformed rows fail with a CLEAN error, not a raw traceback, and write nothing ----------

_HEADER = ["transactionHash", "fromAddress", "fromLabel", "fromIsContract", "toAddress", "toLabel",
           "toIsContract", "tokenAddress", "type", "blockTimestamp", "blockNumber", "blockHash",
           "tokenName", "tokenSymbol", "tokenDecimals", "unitValue", "tokenId", "historicalUSD", "chain"]


def _write_csv(path, rowdict):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_HEADER)
        w.writeheader()
        w.writerow({k: rowdict.get(k, "") for k in _HEADER})
    return path


def _assert_nothing_written(conn):
    for table in ("transfer", "transaction_", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_malformed_unitvalue_is_a_clean_error(case, tmp_path):
    conn, db = case
    bad = _write_csv(tmp_path / "bad_amount.csv", {
        "transactionHash": "0xbad", "chain": "ethereum", "fromAddress": "0x" + "1" * 40,
        "toAddress": "0x" + "2" * 40, "tokenAddress": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "tokenSymbol": "USDT", "tokenDecimals": "6", "unitValue": "not_a_number", "blockNumber": "1"})
    with pytest.raises(ConnectorError) as exc:
        ArkhamImporter().get_transactions(conn, bad)
    assert "unparseable" in str(exc.value)
    _assert_nothing_written(conn)


def test_malformed_address_is_a_clean_error(case, tmp_path):
    conn, db = case
    bad = _write_csv(tmp_path / "bad_addr.csv", {
        "transactionHash": "0xbad2", "chain": "ethereum", "fromAddress": "0xNOTHEX",
        "toAddress": "0x" + "2" * 40, "tokenSymbol": "ETH", "tokenDecimals": "18",
        "unitValue": "1", "blockNumber": "1"})
    with pytest.raises(ConnectorError):
        ArkhamImporter().get_transactions(conn, bad)
    _assert_nothing_written(conn)
