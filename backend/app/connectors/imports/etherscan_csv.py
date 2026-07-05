"""Etherscan CSV export import — normal-transaction ingest (P22/FN-25).

Ingests the Etherscan UI "Download CSV Export" (normal transactions) for an address through the canonical
EVM `transfer` path via the pure `adapt_etherscan_csv` adapter — the SAME `ParsedTransaction`/`ParsedTransfer`
shape the Etherscan **API** connector produces, so the same DB writer resolves ids AND the same on-chain
movement pulled from the API vs this CSV dedups to one row (content+`occurrence`, Invariant #7). The export
file is stored as the import's `source_query.raw_response` (hashed), so provenance holds (Invariant #3):
every imported fact references the import's `source_query`.

The CSV has no chain column (it's explorer-scoped — etherscan.io = ethereum, bscscan.com = bsc, ...), so
`chain` is a parameter (default `ethereum`) and MUST be EVM: an Etherscan export is account-model, and a
UTXO chain is refused up front (Invariant #5 — never synthesize an input->output edge). This importer
handles the NATIVE normal-tx export only; the ERC-20 token export omits `tokenDecimal` (can't derive base
units) and stays a documented follow-up (see the adapter's NOT-done note).
"""

from __future__ import annotations

from ...db import repository as repo
from ...models import Address, Transfer
from ...normalization.canonical import is_evm
from ...normalization.etherscan_csv_adapter import adapt_etherscan_csv
from ...normalization.reconcile import assign_occurrences
from ..base import ConnectorError
from .base import ImportConnector


class EtherscanCsvImporter(ImportConnector):
    name = "etherscan-csv-import"
    source = "etherscan"

    def capabilities(self) -> set[str]:
        return {"get_transactions"}

    def get_transactions(self, conn, file_path, *, chain: str = "ethereum", now=None) -> dict:
        """Ingest an Etherscan normal-tx CSV export into canonical native transfers. Idempotent re-ingest."""
        if not is_evm(chain):
            raise ConnectorError(
                f"Etherscan CSV import chain {chain!r} is not EVM/account-model; an Etherscan export is "
                f"account-model only (Invariant #5). Ingest UTXO chains via the Esplora connector.")
        return self._ingest(
            conn, file_path=file_path, capability="get_transactions",
            parse=lambda c, sqid, raw_bytes, now_: self._parse(c, sqid, raw_bytes, now_, chain),
            now=now, extra_params={"chain": chain})

    @staticmethod
    def _addr(c, sqid, chain, canonical, display=None):
        if not canonical:
            return None  # contract-creation / no counterparty endpoint
        # COR-02: pass the source checksum form; the repository derives the canonical key + preserves it.
        return repo.upsert_address(c, Address(chain=chain, address_display=display or canonical), sqid)

    def _parse(self, c, sqid, raw_bytes, now, chain) -> dict:
        rows = self.read_csv(raw_bytes)
        parsed, notes = adapt_etherscan_csv(
            rows, chain=chain, fieldnames=(list(rows[0].keys()) if rows else []))

        # HARD REFUSE (all-or-nothing — raising rolls the whole import back, nothing written): a corrupt /
        # unrecognized export fails loudly with a clean error, not a raw traceback. row -1 = a header issue.
        if notes["errors"]:
            first = notes["errors"][0]
            where = "the header" if first["row"] == -1 else f"row {first['row']} (tx {first['tx']!r})"
            raise ConnectorError(
                f"Etherscan CSV export has {len(notes['errors'])} unparseable issue(s); first at {where}: "
                f"{first['reason']}. Nothing was imported.")

        assign_occurrences(parsed)  # content+occurrence dedup key (Inv #7) before the DB write
        n_tx = n_tr = 0
        for pt in parsed:
            tx_id = repo.upsert_transaction(c, pt.transaction, sqid)
            n_tx += 1
            for tr in pt.transfers:
                from_id = self._addr(c, sqid, tr.chain, tr.from_address, tr.from_address_display)
                to_id = self._addr(c, sqid, tr.chain, tr.to_address, tr.to_address_display)
                asset_id = repo.upsert_asset(c, tr.asset, sqid)
                repo.upsert_transfer(c, Transfer(
                    transaction_id=tx_id, chain=tr.chain, from_address_id=from_id, to_address_id=to_id,
                    asset_id=asset_id, amount=tr.amount, transfer_type=tr.transfer_type,
                    position=tr.position, occurrence=tr.occurrence), sqid)
                n_tr += 1
        # Surface the signals so nothing is silently dropped: skipped (zero-value/contract calls), failed
        # (reverted — no transfer), and the FN-24 truncation-risk count (display precision < 18 dp).
        return {"transactions": n_tx, "transfers": n_tr, "skipped": notes["skipped"],
                "failed": notes["failed"], "rounded_amounts": notes["rounded_amounts"],
                "truncation_risk": notes["truncation_risk"], "chain": chain}
