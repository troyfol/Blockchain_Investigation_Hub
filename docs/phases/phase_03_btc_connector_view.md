# Phase 3 — Bitcoin connector (Esplora) + value-movement view

> **Invariants (always):** **never synthesize a vin→vout transfer** — Bitcoin stores only
> `tx_input`/`tx_output`; transaction is a visible routing node; input→output linkage is a trace-time
> claim, never a fact. Provenance per call; finality via confirmations. See `CLAUDE.md` §1 #5.

## Goal

Validate the UTXO model and the account/UTXO unification immediately: a real Bitcoin tx → `transaction_`
+ `tx_input`/`tx_output` rows (no fabricated transfers), finality via confirmations, then create the
`v_value_movement` read-model view (already in migration `0005`) and prove it returns **null-src** UTXO
rows.

## Prerequisites

Phases 1 and 2 done.

## Steps

1. **Esplora adapter** (`connectors/esplora.py` + `normalization/esplora_adapter.py`) — `get_transactions`
   (paginate `/address/:a/txs` then `/txs/chain/:last_seen_txid`, 25/page, honor `max_pages`),
   `get_transfers` (`/tx/:txid` → vin/vout), `get_balance` (`/address/:a` chain_stats: funded−spent).
   Tip height via `/blocks/tip/height` for confirmations. Map per `docs/connectors.md` §4.
2. **No-fabrication guard** — the adapter must produce only `tx_input`/`tx_output`; add the audit
   **#5 "no fabricated UTXO edge"** (`SELECT ... v_value_movement WHERE paradigm='utxo' AND src_address_id
   IS NOT NULL` = 0) and wire into `make audit`.
3. **Address canonicalization** for BTC encodings (`docs/algorithms.md` §1); NULL address for
   non-standard scripts.
4. **Cassettes + tests** — record a multi-input/multi-output tx; contract test; golden smoketest
   "Bitcoin transaction-as-node" asserting the `address→transaction→address` shape and the view's
   null-src rows.
5. **Verify the view** — `v_value_movement` returns EVM rows (with src/dst) and BTC rows (src NULL, dst =
   output address, asset = native coin), `finality_status` joined.

## Files to create

`connectors/esplora.py`, `normalization/esplora_adapter.py`, `audits/checks/no_fabricated_utxo_edge.py`,
`tests/contract/test_esplora.py`, `tests/integration/test_btc_golden.py`, `tests/cassettes/esplora/*`.

## Acceptance criteria

- [ ] A golden BTC tx ingests → correct `tx_input`/`tx_output` rows, `transaction_` node, balances from
      chain_stats.
- [ ] No `transfer` rows are created for Bitcoin (guard + audit #5 green).
- [ ] `v_value_movement` returns unified rows; **all** UTXO rows have `src_address_id IS NULL`.
- [ ] Confirmations/finality computed from tip height; provisional vs final correct.
- [ ] Contract test green offline from cassettes.

## Confirm-at-build

- Esplora endpoint paths, vin/vout field names, cursor pagination, mempool.space fallback shape. Log it.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `PROGRESS.md` updated. **This is the hard
gate that validates the central modeling decision — do not proceed until green.**
