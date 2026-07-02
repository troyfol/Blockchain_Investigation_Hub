"""Batch 4 (RES-01 / LOG-05 / EFF-04): the valuation pass must degrade a bad batch to an honest gap, not
abort the whole pass, and must normalize ingest timestamps to the canonical format.

- RES-01: a non-JSON 200 from the price API raises ``ValueError`` (json decode) — the per-batch handler
  only caught ``ConnectorError``, so it escaped and failed the whole job. It must now skip that batch.
- LOG-05: a non-ISO ``block_ts`` reaching ``_iso_to_unix`` raised and aborted the pass — it must skip that
  movement. Root: the Arkham/Bitquery adapters must store a canonical ``YYYY-MM-DDTHH:MM:SSZ`` timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.app.connectors.defillama import PriceRecord
from backend.app.db import repository as repo
from backend.app.models import Asset, SourceQuery, Transaction, TxOutput
from backend.app.normalization.arkham_adapter import adapt_arkham_transfers
from backend.app.normalization.canonical import to_canonical_ts
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.valuation import value_movements
from backend.tests.integration._helpers import new_case


class _FakePrices:
    """A price connector whose ``get_prices`` raises a ValueError (a non-JSON ``.json()``) for one
    timestamp and returns a real price for others."""

    def __init__(self, *, fail_ts=None):
        self.fail_ts = fail_ts
        self.seen = []

    def coin_key(self, chain, asset):
        return f"{chain}:{asset.contract_address}" if asset.contract_address else f"coingecko:{chain}"

    def get_prices(self, items, timestamp):
        self.seen.append(timestamp)
        if timestamp == self.fail_ts:
            raise ValueError("simulated non-JSON 200 body")
        prices = {self.coin_key(ch, a): PriceRecord(key=self.coin_key(ch, a), price="100", symbol="X",
                  decimals=None, price_timestamp=timestamp, confidence=0.9, raw={}) for ch, a in items}
        return prices, {"mock": timestamp}


def _seed_btc_output(conn, *, txid, block_ts, amount="100"):
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        tx = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash=txid, block_height=800000,
                                     block_ts=block_ts, confirmations=20, finality_status="final"), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, amount=amount, output_index=0), sqid)

    write_with_provenance(conn, sq, write)


def test_res01_bad_batch_does_not_abort_pass(tmp_path):
    conn, db = new_case(tmp_path)
    _seed_btc_output(conn, txid="a" * 64, block_ts="2026-01-01T00:00:00Z")   # ts A (will FAIL)
    _seed_btc_output(conn, txid="b" * 64, block_ts="2026-02-01T00:00:00Z")   # ts B (must value)

    fail_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    result = value_movements(conn, _FakePrices(fail_ts=fail_ts))
    # The failing batch is an error+skip; the OTHER batch is still valued (no pass abort).
    assert result["valued"] == 1, "a non-JSON batch aborted the whole pass (RES-01)"
    assert result["errors"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 1
    conn.close()


def test_log05_non_iso_block_ts_is_skipped_not_raised(tmp_path):
    conn, db = new_case(tmp_path)
    _seed_btc_output(conn, txid="c" * 64, block_ts="not-a-timestamp")        # bad ts → skip
    _seed_btc_output(conn, txid="d" * 64, block_ts="2026-03-01T00:00:00Z")   # good ts → value

    result = value_movements(conn, _FakePrices())
    assert result["valued"] == 1, "a good movement was not valued alongside a bad-ts one (LOG-05)"
    assert result["skipped"] >= 1  # the bad-ts movement is an honest skip, not a crash
    conn.close()


def test_log05_to_canonical_ts_normalizer():
    assert to_canonical_ts("2022-06-06T21:48:21Z") == "2022-06-06T21:48:21Z"
    assert to_canonical_ts("2022-06-06T21:48:21+00:00") == "2022-06-06T21:48:21Z"
    assert to_canonical_ts("1654552101") == "2022-06-06T21:48:21Z"  # unix epoch
    assert to_canonical_ts("") is None
    assert to_canonical_ts(None) is None
    assert to_canonical_ts("garbage") is None


def test_log05_arkham_block_ts_is_canonicalized():
    # An Arkham row whose blockTimestamp is a unix epoch must land canonical `...Z` post-adapt (LOG-05 root).
    rows = [{"transactionHash": "0x" + "ab" * 32, "fromAddress": "0x" + "11" * 20,
             "toAddress": "0x" + "22" * 20, "type": "transfer", "tokenSymbol": "ETH",
             "tokenDecimals": "18", "blockNumber": "1", "blockTimestamp": "1654552101",
             "unitValue": "1", "chain": "ethereum"}]
    parsed, _notes = adapt_arkham_transfers(rows)
    assert parsed and parsed[0].transaction.block_ts == "2022-06-06T21:48:21Z"
