# Phase 2 — EVM connector end-to-end (Etherscan V2)

> **Invariants (always):** provenance per call (each of the 3 EVM endpoints = its own `source_query`);
> canonicalize addresses (lowercase); idempotent upsert; bounds recorded in `params`. See `CLAUDE.md` §1.

## Goal

Validate the account-model path and the full provenance loop: a real EVM address → canonical
`transaction_`/`transfer`/`asset`/`balance_snapshot` rows with provenance, idempotent on re-fetch, with
`bounds` honored and recorded.

## Prerequisites

Phase 1 done.

## Steps

1. **Connector base** (`connectors/base.py`) — capability protocol, `Bounds` type, rate-limit token
   bucket + exponential backoff w/ jitter on 429/5xx, `max_pages`, atomic `source_query` writing,
   `partial` status when bounds truncate. (Per `docs/connectors.md` §1.)
2. **Etherscan adapter** (`connectors/etherscan.py` + `normalization/etherscan_adapter.py`) — implement
   `get_transactions` (merge `txlist` + `txlistinternal` + `tokentx`, each paginated, each a
   `source_query`), `get_balance`, `get_transfers`. Map fields per `docs/connectors.md` §2; canonicalize
   addresses; compute finality.
3. **Bounds mapping** — `block_range`→start/endblock; `max_pages`; `time_window`→block range;
   `min_value`/`top_n`/`direction`→post-filter + record in `params`.
4. **Cassettes** — record one real response per endpoint into `tests/cassettes/etherscan/`; these double
   as provenance fixtures.
5. **Contract + bounds tests**; **golden smoketests**: "EVM token transfer" and "EVM idempotency"
   (`docs/testing.md` §3). Wire audit #10 (bounds recorded).

## Files to create

`connectors/base.py`, `connectors/etherscan.py`, `normalization/etherscan_adapter.py`,
`services/orchestrator.py` (dispatch on capability), `tests/contract/test_etherscan.py`,
`tests/integration/test_evm_golden.py`, `tests/cassettes/etherscan/*`.

## Acceptance criteria

- [ ] A golden EVM address ingests → correct `transaction_`/`transfer`(native+internal+erc20)/`asset`
      rows; `(transaction_id, transfer_type, position)` keys collision-free.
- [ ] Each of the 3 endpoints wrote its own `source_query` with `raw_response_hash` and `params` (incl.
      bounds).
- [ ] Re-fetching the same address changes no row counts (upsert; idempotency smoketest green).
- [ ] Bounds (e.g. `block_range`, `max_pages`) limit the pull and are recorded; `partial` set when
      truncated.
- [ ] Addresses stored lowercase-canonical with `address_display` retained.
- [ ] Contract test green offline from cassettes.

## Confirm-at-build

- Etherscan V2 exact field names, rate limits, free-tier **chain coverage** (some chains need paid Lite),
  `status:"0"`=no-records vs error semantics. Log in `PROGRESS.md`.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `PROGRESS.md` updated.
