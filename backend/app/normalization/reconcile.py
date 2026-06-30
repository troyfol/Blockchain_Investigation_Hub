"""Cross-source transfer reconciliation (docs/findings/arkham_export_reconciliation.md decision (c)).

A `transfer` is a FACT. Its identity within a transaction is its CONTENT — `(transfer_type, from, to,
asset, amount)` — NOT its `position` (which is source-dependent: Etherscan = receipt-log order,
Arkham/Bitquery = row order). `assign_occurrences` stamps each transfer with a 0-based `occurrence` among
identical-content movements in its `(tx, transfer_type)`, so the DB's content+occurrence unique key
(migration 0007) makes the SAME on-chain movement ingested from two sources dedup to ONE row regardless
of the order/position each source assigns (Invariant #7), while legitimately-repeated identical movements
are still kept distinct, and genuinely DISAGREEING facts (different parties/amount) stay side-by-side
(different content -> different rows; never collapsed, Invariant #4).

Pure (no HTTP, no DB); call it on the parsed bundles before the connector writes them.
"""

from __future__ import annotations


def assign_occurrences(parsed_txs):
    """Set ``ParsedTransfer.occurrence`` per (tx, transfer_type, from, to, asset, amount) group, in the
    order the source lists them. Idempotent for a given source's output (so re-ingest stays a no-op)."""
    for pt in parsed_txs:
        seen: dict[tuple, int] = {}
        for tr in pt.transfers:
            key = (tr.transfer_type, tr.from_address, tr.to_address,
                   tr.asset.chain, tr.asset.contract_address, tr.amount)
            occ = seen.get(key, 0)
            seen[key] = occ + 1
            tr.occurrence = occ
    return parsed_txs
