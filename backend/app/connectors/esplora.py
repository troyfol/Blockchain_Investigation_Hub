"""Blockstream Esplora connector (phase_03 step 1; docs/connectors.md §4).

Bitcoin/UTXO acquisition. ``get_transactions`` paginates ``/address/:a/txs`` (cursor =
last-seen confirmed txid) and writes ``transaction_`` + ``tx_input``/``tx_output`` rows ONLY —
never a transfer (Invariant #5). Finality uses ``/blocks/tip/height``. When an input spends an
output already in this case DB, that output is marked ``spent`` (allowed even on a final tx —
docs/schema.md §4 / the immutability audit excludes spent/spending_tx_id).

RE-CONFIRMED live 2026-06-28 (was CONFIRMED-AT-BUILD 2026-06-26; docs/findings/
external_facts_confirmation.md): base ``https://blockstream.info/api``; tx list is full tx objects
(no separate /tx call needed); ``/address/:a/txs`` page 1 = up to 50 mempool + first 25 confirmed
(newest first), then ``/address/:a/txs/chain/:last_txid`` for 25 confirmed/page; values in satoshis.
Mempool (unconfirmed) txs flow through and are marked ``provisional`` (block_height NULL).
"""

from __future__ import annotations

from .base import BaseHttpConnector, UpstreamError, filter_supported_bounds
from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Address, Asset, BalanceSnapshot, SourceQuery, TxInput, TxOutput
from ..normalization.canonical import canonical_address
from ..normalization.esplora_adapter import (
    BTC_DECIMALS,
    BTC_NATIVE_SYMBOL,
    adapt_address_txs,
    balance_from_stats,
)
from ..provenance.atomic import write_with_provenance

CONFIRMED_PAGE_SIZE = 25  # Esplora returns 25 confirmed txs per page
SUPPORTED_BOUNDS = {"max_pages"}  # address/txs has no block/time filter; honor max_pages via cursor


class EsploraConnector(BaseHttpConnector):
    name = "esplora"

    def __init__(self, *, settings, base_url: str | None = None, **kw):
        super().__init__(base_url=base_url or settings.esplora_base_url, **kw)
        self.settings = settings

    def capabilities(self) -> set[str]:
        return {"get_transactions", "get_balance", "get_transfers"}

    def supported_chains(self) -> set[str]:
        return {"bitcoin"}

    def tip_height(self, chain: str) -> int:
        """Current chain tip height (``/blocks/tip/height``). Public so the finality-refresh service can
        re-evaluate provisional facts against the live tip (services/finality.py; Invariant #6)."""
        text = self.request(path="/blocks/tip/height").text.strip()
        try:
            return int(text)
        except ValueError:
            # A 200 with a non-numeric body (captive portal / proxy HTML) -> a typed connector error.
            raise UpstreamError(f"Esplora tip-height returned a non-integer body: {text[:80]!r}")

    def _collect_txs(self, address: str, bounds: dict):
        """Cursor-paginate the address tx history. Returns (payloads, txs, partial)."""
        max_pages = bounds.get("max_pages")
        payloads, txs = [], []
        partial = False
        page = 1
        last_confirmed = None
        while True:
            path = (f"/address/{address}/txs" if last_confirmed is None
                    else f"/address/{address}/txs/chain/{last_confirmed}")
            payload = self.request(path=path).json()
            payloads.append(payload)
            txs.extend(payload)
            confirmed = [t for t in payload if (t.get("status") or {}).get("confirmed")]
            if len(confirmed) < CONFIRMED_PAGE_SIZE:
                break  # last page
            last_confirmed = confirmed[-1]["txid"]
            page += 1
            if max_pages is not None and page > max_pages:
                partial = True
                break
        return payloads, txs, partial

    # --- writers -------------------------------------------------------------------------

    def _addr_id(self, conn, sqid, chain, canonical):
        if not canonical:
            return None
        return repo.upsert_address(conn, Address(chain=chain, address_display=canonical), sqid)

    def _resolve_prev_output(self, conn, prev_txid, prev_vout, spending_tx_id):
        """If the spent output is in this DB, return its id and mark it spent (linkage refresh)."""
        if prev_txid is None or prev_vout is None:
            return None
        # EFF-02: constrain the (constant, known) chain so ux_transaction(chain, tx_hash) is used as an
        # indexed seek — without it, `WHERE tx_hash=?` alone can't use the index (leftmost col = chain)
        # and SQLite full-SCANs tx_output per input (O(inputs × |tx_output|) on a BTC-heavy re-ingest).
        row = conn.execute(
            "SELECT o.id FROM tx_output o JOIN transaction_ t ON t.id=o.transaction_id "
            "WHERE t.chain='bitcoin' AND t.tx_hash=? AND o.output_index=?",
            (prev_txid, prev_vout)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE tx_output SET spent=1, spending_tx_id=? WHERE id=?", (spending_tx_id, row["id"]))
        return row["id"]

    def _write_btc(self, conn, sqid, chain, parsed) -> dict:
        # Native coin asset so v_value_movement's UTXO rows resolve a native asset_id.
        repo.upsert_asset(conn, Asset(chain=chain, contract_address=None,
                                      symbol=BTC_NATIVE_SYMBOL, decimals=BTC_DECIMALS), sqid)
        n_tx = n_in = n_out = 0
        # Two passes so intra-batch spend linkage is independent of Esplora's newest-first ordering:
        # write EVERY transaction's outputs first, THEN resolve inputs against the now-complete output
        # set. (Single-pass would leave an address's own internal spend-chain unlinked when the spending
        # tx is streamed before the funding tx it draws on — surfaced by the Colonial Pipeline case.)
        written = []
        for pt in parsed:
            tx_id = repo.upsert_transaction(conn, pt.transaction, sqid, authoritative=True)
            n_tx += 1
            for o in pt.outputs:
                repo.upsert_tx_output(conn, TxOutput(
                    transaction_id=tx_id, address_id=self._addr_id(conn, sqid, o.chain, o.address),
                    amount=o.amount, output_index=o.output_index), sqid)
                n_out += 1
            written.append((pt, tx_id))
        for pt, tx_id in written:
            for i in pt.inputs:
                prev_id = self._resolve_prev_output(conn, i.prev_txid, i.prev_vout, tx_id)
                repo.upsert_tx_input(conn, TxInput(
                    transaction_id=tx_id, prev_output_id=prev_id,
                    address_id=self._addr_id(conn, sqid, i.chain, i.address), amount=i.amount,
                    input_index=i.input_index), sqid)
                n_in += 1
        return {"transactions": n_tx, "inputs": n_in, "outputs": n_out}

    # --- capabilities --------------------------------------------------------------------

    def get_transactions(self, conn, chain: str, address: str, bounds: dict | None = None) -> dict:
        # TOLERANT bounds (P8.6): apply the bounds Esplora supports (max_pages) and SKIP any it doesn't
        # (e.g. an EVM-only top_n_counterparties from the chain-agnostic depth control) — recorded in
        # params + the query marked partial — instead of hard-erroring and aborting the BTC ingest.
        applied, skipped = filter_supported_bounds(bounds, SUPPORTED_BOUNDS)
        address = canonical_address(chain, address)
        tip = self.tip_height(chain)
        threshold = self.settings.finality_threshold(chain)
        payloads, txs, collect_partial = self._collect_txs(address, applied)
        parsed = adapt_address_txs(txs, chain=chain, tip_height=tip, threshold=threshold)
        partial = collect_partial or bool(skipped)
        now = utc_now_iso()
        params = {"address": address, "chain": chain, "bounds": dict(applied) if applied else "default"}
        if skipped:
            params["skipped_bounds"] = skipped  # bounds Esplora can't apply (recorded for reproducibility)
        sq = SourceQuery(connector=self.name, capability="get_transactions", endpoint="address-txs",
                         params=params, requested_at=now, completed_at=now,
                         status="partial" if partial else "ok",
                         result_summary=f"{len(txs)} txs" + (f"; skipped bounds {skipped}" if skipped else ""))
        # COR-01: a COMPLETE re-fetch (not bounded/partial) whose fresh set omits a stored PROVISIONAL tx
        # means that tx was reorged/replaced — sweep it (+ children) under this fetch's source_query. A
        # partial page legitimately omits txs, so the sweep is gated on `not partial`.
        present = {pt.transaction.tx_hash for pt in parsed}

        def _writer(c, sqid):
            res = self._write_btc(c, sqid, chain, parsed)
            if not partial:
                swept = repo.sweep_reorged_provisional(
                    c, chain=chain, address=address, present_tx_hashes=present, source_query_id=sqid)
                if swept["deleted"] or swept["skipped_referenced"]:
                    res["reorged"] = swept
            return res

        _, res = write_with_provenance(conn, sq, _writer, raw_response=payloads)
        return res

    def get_transfers(self, conn, chain: str, txid: str) -> dict:
        """tx-scoped: fetch one tx's full detail and ingest its inputs/outputs."""
        tx = self.request(path=f"/tx/{txid}").json()
        tip = self.tip_height(chain)
        threshold = self.settings.finality_threshold(chain)
        parsed = adapt_address_txs([tx], chain=chain, tip_height=tip, threshold=threshold)
        now = utc_now_iso()
        sq = SourceQuery(connector=self.name, capability="get_transfers", endpoint="tx",
                         params={"txid": txid, "chain": chain}, requested_at=now, completed_at=now,
                         status="ok")
        _, res = write_with_provenance(
            conn, sq, lambda c, sqid: self._write_btc(c, sqid, chain, parsed), raw_response=tx)
        return res

    def get_balance(self, conn, chain: str, address: str) -> str:
        address = canonical_address(chain, address)
        payload = self.request(path=f"/address/{address}").json()
        balance = balance_from_stats(payload)
        now = utc_now_iso()
        sq = SourceQuery(connector=self.name, capability="get_balance", endpoint="address-stats",
                         params={"address": address, "chain": chain, "bounds": "default"},
                         requested_at=now, completed_at=now, status="ok")

        def write(c, sqid):
            addr_id = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            return repo.insert_balance_snapshot(c, BalanceSnapshot(
                address_id=addr_id, asset_id=None, amount=str(balance), as_of_ts=now,
                source=self.name, retrieved_at=now), sqid)

        _, bid = write_with_provenance(conn, sq, write, raw_response=payload)
        return bid
