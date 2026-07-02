"""Batch 7 (LOG-10 / COR-02): EVM/Arkham adapter ingest fidelity.

- LOG-10: an Arkham multichain export with the SAME tx hash on two chains must produce two distinct
  ``transaction_`` rows (keyed on ``(chain, tx_hash)``), each transfer's ``chain`` matching its tx's chain
  — not one tx absorbing the other's transfers.
- COR-02: an EIP-55-checksummed EVM address ingested via Bitquery/Arkham-CSV must survive in
  ``address_display`` with its checksum, while ``canonical_address(chain, address_display) == address``.
"""

from __future__ import annotations

from backend.app.connectors.imports.arkham import ArkhamImporter
from backend.app.db import repository as repo
from backend.app.models import SourceQuery
from backend.app.normalization.arkham_adapter import adapt_arkham_transfers
from backend.app.normalization.canonical import canonical_address
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

# A mixed-case (EIP-55-style) address — the checksum must survive ingest.
CHECKSUMMED = "0xAbC0000000000000000000000000000000000123"
OTHER = "0xdEf0000000000000000000000000000000000456"


def test_log10_multichain_hash_stays_distinct():
    replayed = "0x" + "ab" * 32
    rows = [
        {"transactionHash": replayed, "fromAddress": CHECKSUMMED, "toAddress": OTHER, "type": "transfer",
         "tokenSymbol": "ETH", "tokenDecimals": "18", "blockNumber": "1",
         "blockTimestamp": "2022-01-01T00:00:00Z", "unitValue": "1", "chain": "ethereum"},
        {"transactionHash": replayed, "fromAddress": CHECKSUMMED, "toAddress": OTHER, "type": "transfer",
         "tokenSymbol": "BNB", "tokenDecimals": "18", "blockNumber": "2",
         "blockTimestamp": "2022-01-01T00:00:00Z", "unitValue": "2", "chain": "bsc"},
    ]
    parsed, _notes = adapt_arkham_transfers(rows)
    # Two DISTINCT transactions, one per chain — not one absorbing the other.
    assert len(parsed) == 2, "a replayed hash across chains collapsed into one tx (LOG-10)"
    chains = sorted(pt.transaction.chain for pt in parsed)
    assert chains == ["bsc", "ethereum"]
    for pt in parsed:
        for tr in pt.transfers:
            assert tr.chain == pt.transaction.chain, "transfer.chain diverged from transaction_.chain (LOG-10)"


def test_cor02_checksum_survives_arkham_ingest(tmp_path):
    conn, db = new_case(tmp_path)
    rows = [{"transactionHash": "0x" + "cd" * 32, "fromAddress": CHECKSUMMED, "toAddress": OTHER,
             "type": "transfer", "tokenSymbol": "ETH", "tokenDecimals": "18", "blockNumber": "1",
             "blockTimestamp": "2022-01-01T00:00:00Z", "unitValue": "1", "chain": "ethereum"}]
    importer = ArkhamImporter()

    sq = SourceQuery(connector="arkham-import", capability="get_transactions", endpoint="import",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    write_with_provenance(conn, sq, lambda c, sqid: importer._parse(c, sqid, _rows_to_csv(rows), "now"))

    # The checksum survives in address_display AND canonicalizes back to the stored canonical key.
    for chain, address, display in conn.execute(
            "SELECT chain, address, address_display FROM address").fetchall():
        assert canonical_address(chain, display) == address, "address_display != source form (COR-02)"
    disp = {r[0] for r in conn.execute("SELECT address_display FROM address").fetchall()}
    assert CHECKSUMMED in disp, "the EIP-55 checksum was dropped on Arkham ingest (COR-02)"
    assert canonical_address("ethereum", CHECKSUMMED) != CHECKSUMMED  # sanity: canonical IS lowercased
    conn.close()


def _rows_to_csv(rows) -> bytes:
    import csv
    import io

    cols = ["transactionHash", "fromAddress", "fromLabel", "fromIsContract", "toAddress", "toLabel",
            "toIsContract", "tokenAddress", "type", "blockTimestamp", "blockNumber", "blockHash",
            "tokenName", "tokenSymbol", "tokenDecimals", "unitValue", "tokenId", "historicalUSD", "chain"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue().encode("utf-8")
