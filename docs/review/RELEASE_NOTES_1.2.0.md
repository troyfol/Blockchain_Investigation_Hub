# Blockchain Investigation Hub — v1.2.0

**Release date:** 2026-07-02 · Previous release: v1.0.0.

v1.2.0 is a **hardening + reachability** release. It closes the findings of a full five-lens internal
review (security · correctness · resilience · logic · efficiency), wires up two capabilities that were
implemented but unreachable, and ships a reproducible dependency lockfile. **No schema change** — existing
cases open unchanged (see *Upgrade notes*).

The review found **no deep structural flaw**: the load-bearing invariants (provenance written atomically
with every fact, no fabricated UTXO edges, idempotent ingest, address canonicalization, Decimal money
math, parameterized SQL everywhere) were traced to enforcing code and verified clean. The fixes below
close localized defects in three seams — the untrusted input/output boundary, re-fetch/upsert edge
semantics, and reachability gaps.

---

## Security fixes

Described at the risk level; reproduction detail is intentionally omitted.

- **Report rendering can no longer execute or forge content from hostile on-chain text (SEC-01, was
  Critical).** Attacker-controlled strings that ride in with the data — a token symbol, an imported
  attribution name, an investigator label — are now fully escaped when the court-facing report is built,
  so they cannot run script or silently alter the rendered exhibit. The report is the flagship
  evidentiary artifact; this was the most important fix in the release.
- **The localhost API now validates request provenance (SEC-02/04/06/17, was High).** The single-user
  local API rejects requests that don't originate from the app's own origin (Host/Origin defense), closing
  a DNS-rebinding avenue by which a web page visited while the app was running could have read or rewritten
  case data, keys, or settings.
- **`.casefile` import is hardened before anything is trusted (SEC-05/16).** Bundle extraction is now
  bounded and path-safe (no traversal, no zip-bomb blowup) *before* any hash/manifest check runs; a
  malformed or hostile bundle is rejected cleanly rather than partially extracted.
- **Upstream errors never leak the API key or a raw stack (SEC-03).** Connector/HTTP failures are
  sanitized — the key-bearing URL is redacted and the app returns a clean error, honoring the "never a
  raw 500" contract.
- **Keyring availability is surfaced loudly (SEC-08).** If no OS keyring backend is available (or the
  dev-only plaintext-key opt-in is active), the app shows an explicit banner instead of silently degrading
  secret storage.
- **The OFAC/intel refresh goes through the isolated connector layer (SEC-13)**, keeping all outbound HTTP
  confined to the connector package.
- **Reproducible builds (SEC-14).** A fully-pinned `requirements.lock` is shipped so a build can be
  reproduced exactly.

## Correctness & resilience fixes

- **Re-ingest no longer silently loses or corrupts facts.** Re-fetching a Bitcoin funding transaction no
  longer resets its outputs to *unspent* (LOG-01); a second source can no longer clobber a token's
  `decimals` and silently rescale every USD figure by a power of ten (LOG-12); and a lower-priority feed
  can no longer overwrite the authoritative transaction status on a provisional row (LOG-11/13). Idempotent
  re-ingest (Invariant #7) now holds across these edge cases.
- **Reorg handling cascades cleanly (COR-01).** When a provisional transaction is dropped by a re-org, its
  machine-derived dependents (valuations, machine trace links) are removed with it, while anything an
  investigator personally annotated, tagged, labelled, or cited in a finding is **preserved and reported**,
  never silently deleted.
- **The valuation pass rides out bad data and rate limits (RES-01).** A single malformed price response or
  timestamp no longer aborts the whole pass — it skips the affected movement (an honest gap, never a
  fabricated zero) and values the rest.
- **Writes are atomic (RES-03/04).** Provenance files are durably written before the DB commit that
  references them, and `.casefile`/manifest export uses a temp-then-rename so a crash can't leave a
  half-written bundle; orphaned scratch files are swept at case open.
- **Schema version is consistent and forward-guarded (BASE-02/03, LOG-02).** The migration runner now
  correctly stamps `schema_version`, and opening a case created by a *newer* build fails loudly instead of
  operating on a schema it doesn't understand.
- **Higher ingest fidelity (Batch 7).** EVM/Arkham adapter edge cases (position conventions, value
  precision flags) were tightened.

## New functionality & UX

- **Trace construction is now reachable end to end (LOG-04/07).** The trace-building service
  (create a trace, add an EVM transfer, FIFO-apportion a Bitcoin transaction, add a manual link) is now
  exposed via `POST /api/trace*` **and** a **Trace-builder panel** in the investigation UI — previously it
  existed only in unit tests and could never be built in the shipped app.
- **ERC-20 approval fetch + self-authorization heuristic (LOG-06).** Approval events can now be fetched on
  demand via `POST /api/approvals/fetch` (Etherscan `getLogs`), which populates the `erc20_approval` table
  so the EVM self-authorization clustering heuristic can actually fire (it was previously a permanent
  no-op). It remains an honest no-op when there is no approval data — never a fabricated link.
- **Faster hot paths (EFF-01/02/03).** The node-summary endpoint is scoped to the relevant neighborhood,
  chain-indexed lookups replace scans, and the entity merge-forest is batch-loaded — the graph build and
  summary stay responsive on dense cases, with identical output.
- **Config cleanup (LOG-08).** Dead settings (`cache_ttl_days`, `etherscan_paid_tier`) that had no effect
  were removed.

## Known remaining issues

- **`build_view` deep query (deferred by choice).** The focus/hop/cap logic is not yet pushed fully into a
  single recursive SQL query; current behavior is correct and acceptable at the intended case scale. A
  future rewrite (behind golden tests) is the one deliberately-deferred review item.
- **Arkham EVM amounts are display-precision until chain-confirmed.** Amounts imported from an Arkham UI
  export can be low-order-lossy; treat them as approximate until re-fetched from chain. (Documented
  follow-up; not a regression.)
- **MisTrack import schema is assumed.** The MisTrack CSV column mapping is still the assumed layout,
  pending confirmation against a real export.
- **Bulk valuation is rate-limited by the free price tier.** DeFiLlama's free tier throttles high-volume
  historical pricing; large cases value in bounded batches and may show honest coverage gaps until priced
  on a fresh window. Prices themselves are exact where present.

All open, actionable findings from the internal review were addressed except the deferred `build_view`
rewrite above.

## Upgrade notes (existing case DBs)

- **No schema change.** `CURRENT_SCHEMA_VERSION` is still **6** and v1.2.0 adds **no new migrations**
  (`0001–0010` are unchanged from v1.0.0). A case created by v1.0.0 opens directly in v1.2.0 with **no data
  migration**.
- **Schema-version re-stamp on open.** The migration runner now re-stamps `schema_version = 6` when it
  opens an existing case, correcting the earlier inconsistency where fully-migrated DBs could report a
  stale version. This is a metadata correction only — no fact rows are touched.
- **Forward-compatibility guard.** A case created by a *future* build with a higher schema version will now
  refuse to open rather than run against an unknown schema. This only affects cases from builds newer than
  the one opening them.
- **Existing `.casefile` bundles remain valid.** They re-import and re-verify (hash manifest + audits)
  unchanged; immutability baselines are unaffected.
- **No dependency changes.** The runtime dependency set is identical to v1.0.0; exact versions are now
  pinned in `requirements.lock`.
