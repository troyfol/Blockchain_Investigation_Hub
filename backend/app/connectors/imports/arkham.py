"""Arkham UI export import — TRANSFER LOG ingest (Phase 7; re-scoped 2026-06-28).

The Arkham logged-in "download" export is a **transfer log**, not an attribution list — see
`docs/findings/arkham_export_reconciliation.md` and `docs/connectors.md §6`. This importer ingests it
through the canonical EVM `transfer` path (`transaction_` / `transfer` / `asset` / `address`) via the
pure `adapt_arkham_transfers` adapter — the same `ParsedTransaction`/`ParsedTransfer` shape the Etherscan
connector produces, so the same DB writer resolves ids. The export file is stored as the import's
`source_query.raw_response` (hashed), so provenance holds (Invariant #3).

It does **not** produce attributions: address→entity/label/confidence is not in any UI export; that needs
Arkham's official API (Path B, Invariant #1 — no scraping). `from/toLabel` are unreliable party labels
(often bare addresses), so we never synthesize attributions from them.
"""

from __future__ import annotations

from ...db import repository as repo
from ...models import Address, Transfer, Valuation
from ...normalization.arkham_adapter import adapt_arkham_transfers
from ...normalization.reconcile import assign_occurrences
from ...normalization.valuation_math import unit_price_from_total
from ..base import ConnectorError
from .base import ImportConnector


class ArkhamImporter(ImportConnector):
    name = "arkham-import"
    source = "arkham"

    def capabilities(self) -> set[str]:
        return {"get_transactions"}

    def get_transactions(self, conn, file_path, *, now=None) -> dict:
        """Ingest an Arkham transfer-log CSV export into canonical transfers. Idempotent re-ingest."""
        return self._ingest(conn, file_path=file_path, capability="get_transactions",
                            parse=self._parse, now=now)

    @staticmethod
    def _addr(c, sqid, chain, canonical, display=None):
        if not canonical:
            return None  # mint/burn — no counterparty address
        # COR-02: pass the source checksum form; the repository derives the canonical key + preserves it.
        return repo.upsert_address(c, Address(chain=chain, address_display=display or canonical), sqid)

    @staticmethod
    def _record_valuation(c, sqid, transfer_id, tr, block_ts, now) -> int:
        """FN-18: record Arkham's per-transfer ``historicalUSD`` as a SECOND sourced valuation
        (``source='arkham'``) on the movement — alongside DeFiLlama, never merged (Invariant #4). The
        source states the movement's *total* USD value (stored verbatim as ``value``); ``unit_price`` is
        derived. Writes NO row (honest gap) when the source didn't price it, there's no block timestamp to
        anchor the price at, or the amount is zero. Idempotent (Invariant #7): a prior identical ingest
        already recorded an arkham valuation on this movement → no-op. Returns 1 if a row was written."""
        if not tr.historical_usd or not block_ts:
            return 0
        unit_price = unit_price_from_total(tr.historical_usd, tr.amount, tr.asset.decimals)
        if unit_price is None:
            return 0  # zero amount → no derivable unit price
        if c.execute("SELECT 1 FROM valuation WHERE subject_type='transfer' AND subject_id=? "
                     "AND source='arkham' LIMIT 1", (transfer_id,)).fetchone():
            return 0
        repo.insert_valuation(c, Valuation(
            subject_type="transfer", subject_id=transfer_id, currency="USD", unit_price=unit_price,
            value=tr.historical_usd, price_timestamp=block_ts, confidence=None, source="arkham",
            retrieved_at=now), sqid)
        return 1

    def _parse(self, c, sqid, raw_bytes, now) -> dict:
        parsed, notes = adapt_arkham_transfers(self.read_csv(raw_bytes))

        # HARD REFUSE (all-or-nothing — raising rolls the whole import back, nothing written):
        # (1) UTXO rows (Bitcoin): Arkham collapses a tx's UTXO input set into a single (often
        #     multi-address) from→to pair; writing that as a `transfer` would fabricate an input→output
        #     edge (Invariant #5). Ingest Bitcoin via the Esplora connector instead.
        if notes["rejected_utxo"]:
            chains = sorted({r["chain"] for r in notes["rejected_utxo"]})
            raise ConnectorError(
                f"Arkham export has {len(notes['rejected_utxo'])} UTXO row(s) on {chains}; refusing to "
                f"ingest — a UTXO from→to pair would fabricate an input→output edge (Invariant #5). "
                f"Ingest Bitcoin via the Esplora connector, not this transfer-log export.")
        # (2) malformed rows (bad amount/address/block): a corrupt export fails loudly, not a raw traceback.
        if notes["errors"]:
            first = notes["errors"][0]
            raise ConnectorError(
                f"Arkham export has {len(notes['errors'])} unparseable row(s); first at row "
                f"{first['row']} (tx {first['tx']!r}): {first['reason']}. Nothing was imported.")

        # SKIP-AND-REPORT (NOT a fabrication risk): account-model chains we don't model yet (e.g. Tron)
        # have no canonical-address handling; their rows are skipped and surfaced, while the supported EVM
        # rows in the same (multichain) export are still ingested.
        assign_occurrences(parsed)  # content+occurrence dedup key (decision (c)) before the DB write
        n_tx = n_tr = n_val = 0
        for pt in parsed:
            tx_id = repo.upsert_transaction(c, pt.transaction, sqid)
            n_tx += 1
            for tr in pt.transfers:
                from_id = self._addr(c, sqid, tr.chain, tr.from_address, tr.from_address_display)
                to_id = self._addr(c, sqid, tr.chain, tr.to_address, tr.to_address_display)
                asset_id = repo.upsert_asset(c, tr.asset, sqid)
                transfer_id = repo.upsert_transfer(c, Transfer(
                    transaction_id=tx_id, chain=tr.chain, from_address_id=from_id, to_address_id=to_id,
                    asset_id=asset_id, amount=tr.amount, transfer_type=tr.transfer_type,
                    position=tr.position, occurrence=tr.occurrence), sqid)
                n_tr += 1
                # FN-18: route the row's `historicalUSD` into a second sourced valuation on this movement.
                n_val += self._record_valuation(c, sqid, transfer_id, tr, pt.transaction.block_ts, now)
        # Surface the open-decision signals + the skipped unsupported chains so nothing is silently dropped.
        return {"transactions": n_tx, "transfers": n_tr, "valuations": n_val, "skipped": notes["skipped"],
                "rounded_amounts": notes["rounded_amounts"],
                # FN-24: Arkham DISPLAY amounts whose precision fell below the asset's decimals — low-order-
                # lossy, flagged so they're never taken as chain-exact (re-fetch the chain for the exact value).
                "truncation_risk": notes["truncation_risk"], "type_present": notes["type_present"],
                "unsupported_skipped": len(notes["rejected_unsupported"]),
                "unsupported_chains": sorted({r["chain"] for r in notes["rejected_unsupported"]})}
