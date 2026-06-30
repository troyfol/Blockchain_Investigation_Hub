# Testing & Audit Strategy

The goal: the project **stays working as it grows**. We get there with layered tests + runnable
invariant audits + per-phase green gates. Nothing is "done" until `make test`, `make audit`, and
`make smoke` are all green (`CLAUDE.md` §5).

## 1. Test layers

| Layer | Location | What it covers | Hits network? |
|---|---|---|---|
| Unit | `tests/unit/` | pure functions: canonicalization, decimal/amount math, FIFO apportionment, finality calc, union-find | no |
| Contract (cassette) | `tests/contract/` + `tests/cassettes/` | each connector's adapter maps a **recorded raw response** → expected canonical rows | no (replays cassettes) |
| Integration (golden) | `tests/integration/` + `tests/fixtures/` | end-to-end: ingest a golden subject → DB rows + provenance → read-model/queries | no (uses cassettes) |
| Property | `tests/property/` | invariants over generated inputs (Hypothesis): value conservation, no-duplicate idempotency, FIFO sums | no |
| Live drift (manual/scheduled) | `tests/contract/test_live_drift.py` | re-hit real APIs to confirm response **shape** hasn't changed; refresh cassettes | yes (opt-in, `RUN_LIVE=1`) |

**Cassettes double as provenance fixtures.** Because the tool stores raw responses anyway, record real
responses once (Phase 2/3), commit them under `tests/cassettes/`, and replay them. Deterministic,
offline, and faithful to real payloads. Refresh via the live-drift test when shapes change.

## 2. Invariant audits (`make audit`) — the safety net

`backend/app/audits/` implements runnable checks against any `case.db`. These encode the Invariants
(`CLAUDE.md` §1) as queries that FAIL LOUDLY. Run after any data write and in CI. Each check returns
pass/fail + offending rows.

Required checks (add the phase that introduces the relevant tables):

1. **Provenance completeness** — every row in Family A and Family B has a resolvable `source_query_id`,
   except whitelisted investigator-authored claim rows (attribution/membership with
   `source='investigator'`). *(Phase 1)*
2. **No dangling FKs** — `PRAGMA foreign_key_check` returns empty; plus the app-enforced poly refs
   (`valuation.subject_id`, `finding_ref.ref_id`, `annotation.target_id`, `tag.target_id`,
   `entity.canonical_membership_id`) all resolve to an existing row of the declared type. *(Phase 1/4/6/8)*
3. **Idempotency** — no duplicate natural keys in `asset/address/transaction_/transfer/tx_input/
   tx_output` (the unique indexes enforce this; the audit asserts counts match distinct keys). *(Phase 1)*
4. **Final-immutability** — a stored checksum of all `final` transactions + their children does not
   change between runs; any change to a `final` row is a failure. (Provisional rows may change.) *(Phase 1)*
5. **No fabricated UTXO edge** — `SELECT COUNT(*) FROM v_value_movement WHERE paradigm='utxo' AND
   src_address_id IS NOT NULL` must be 0. This is Invariant #5 as a test. *(Phase 3)*
6. **Append-only claims** — claim tables only grow: a snapshot of `(id)` sets from the previous audit is
   a subset of the current set (no deletions/rewrites of attribution/risk/valuation/balance/
   entity_membership). *(Phase 1/5/6/7)*
7. **Entity resolution sanity** — no cycle in `entity.merged_into` (follow pointers, detect loops);
   `canonical_membership_id`, when set, references a membership whose `entity_id` resolves (through any
   `merged_into` chain) to this entity. *(Phase 6)*
8. **Cache provenance carried** — for every claim copied from the shared cache, its `source_query` row
   exists in this `case.db` (no claim with a `source_query_id` pointing at a missing query). *(Phase 1)*
9. **Valuation subject validity** — every `valuation` points at an existing `transfer` or `tx_output`
   matching `subject_type`. *(Phase 5)*
10. **Bounds recorded** — every `source_query` for an address-scoped capability has a `params` JSON that
    includes the applied bounds (or an explicit `"bounds":"default"`), so partiality is reproducible.
    *(Phase 2)*

`make audit` exits non-zero if any check fails and prints the offending rows.

## 3. Golden smoketests (`make smoke`)

End-to-end checks against **real on-chain fixtures** with known properties, replayed from cassettes.
Record these during the relevant phase (the txids/addresses are sourced at build time — confirm they
still have the expected shape):

- **EVM token transfer** — an address with a known ERC-20 transfer; assert native+internal+token merge,
  correct `(transfer_type, position)` keys, provenance rows. *(Phase 2)*
- **EVM idempotency** — ingest the same address twice; row counts identical (upsert, no dupes). *(Phase 2)*
- **Bitcoin transaction-as-node** — a multi-input/multi-output tx; assert tx_input/tx_output rows,
  `address→transaction→address` shape, view returns null-src UTXO rows. *(Phase 3)*
- **Known co-spend cluster** — a tx whose inputs are a known cluster; assert membership rows with
  `method=co-spend`. *(Phase 6)*
- **Known CoinJoin** — a Whirlpool/Wasabi-style equal-output tx; assert co-spend memberships carry
  `flags='possible-coinjoin'`. *(Phase 6)*
- **FIFO trace** — a small known chain; assert FIFO apportionment matches a hand-computed expected, and
  conservation holds (apportioned ≤ source). *(Phase 8)*
- **Report + export** — generate a report, export a `.casefile`, verify `manifest.json` hashes match and
  the bundle re-opens self-contained. *(Phase 9/10)*

## 4. Property tests (Hypothesis)

- **Value conservation:** for any FIFO apportionment of a transaction, sum of apportioned amounts ≤ the
  source output amount; no negative apportionment.
- **Decimal correctness:** `value = unit_price × amount / 10^decimals` round-trips within the defined
  precision; no float drift (use `Decimal`, half-even, 18 sig digits).
- **Canonicalization idempotence:** `canonical(canonical(x)) == canonical(x)`; distinct inputs that are
  the same address canonicalize equal (EVM checksum vs lowercase).
- **Upsert idempotence:** applying the same canonical record N times yields exactly one row.

## 5. CI & regression gates

- CI runs `make test && make audit && make smoke` on every change.
- A phase's tests are **additive** — CI always runs all prior phases' tests; a regression blocks the
  phase. The phase is not done while any earlier test/audit fails.
- Keep cassettes committed so CI is offline-deterministic. The live-drift test runs only on demand
  (`RUN_LIVE=1`) or on a schedule, and is allowed to fail without blocking (it signals "refresh
  cassettes / confirm docs").

## 6. Per-phase Definition of Done (test obligations)

Every phase must, before exit: add unit tests for new pure logic; add/extend a contract test for any new
connector adapter; add a golden smoketest for the phase's headline capability; wire any new invariant
audit from §2; and confirm `make test && make audit && make smoke` are green with no regression. Record
completion + any confirmed-volatile facts in `PROGRESS.md`.
