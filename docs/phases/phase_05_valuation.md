# Phase 5 — Valuation (DeFiLlama, value-at-time)

> **Invariants (always):** valuation is a **sourced, derived claim**, never a bare number; value each
> movement at **its block timestamp**; missing/low-confidence valuations are represented honestly, never
> fabricated. Append-only. See `CLAUDE.md` §1 #3/#4.

## Goal

Attach value-at-time (USD) to value movements — `transfer` (EVM) and `tx_output` (Bitcoin) — as
`valuation` claims with confidence and provenance.

## Prerequisites

Phase 3 done. (Independent of Phase 4; either order.)

## Steps

1. **DeFiLlama connector** (`connectors/defillama.py`) — `get_price(chain, asset, timestamp)` against
   `/prices/historical/{ts}/{coins}`; resolve native-coin keys per chain (CONFIRM); parse
   `price/confidence/decimals/symbol`.
2. **Valuation service** (`services/valuation.py`) — for a movement, fetch price at the block timestamp,
   compute `value` per `docs/algorithms.md` §3 (Decimal, half-even, 18 sig), write a `valuation` row with
   provenance. Subject = `transfer` (EVM) or `tx_output` (BTC). Skip (no row) when price missing — never
   write a fabricated zero.
3. **Coverage honesty** — surface missing/low-confidence in the API/UI; `v_address_flow` already
   LEFT JOINs valuation so unvalued movements show null USD.
4. **Tests** — unit (Decimal precision, property test for no float drift); contract (cassette); audit #9
   (valuation subject validity).

## Files to create

`connectors/defillama.py`, `services/valuation.py`, `audits/checks/valuation_subject.py`,
`tests/unit/test_valuation_precision.py`, `tests/property/test_value_conservation.py`,
`tests/contract/test_defillama.py`, `tests/cassettes/defillama/*`.

## Acceptance criteria

- [ ] Movements valued at block timestamp; `valuation` carries `confidence`, `price_timestamp`, provenance.
- [ ] Missing price → no valuation row (honest gap), not a zero.
- [ ] Decimal math exact; property test green.
- [ ] Audit #9 green; append-only preserved (re-valuing adds a row, never overwrites).

## Confirm-at-build

- DeFiLlama endpoint shape, **native-coin key format** (e.g. `coingecko:bitcoin`/`coingecko:ethereum`),
  rate limits. Log it.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `PROGRESS.md` updated.
