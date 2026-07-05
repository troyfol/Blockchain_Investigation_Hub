# Validation case — LEA/FIU court-ready deliverable chain (Operation Ledger) · EVM · **synthetic**

**Purpose:** validate the court-ready OUTPUT chain end-to-end — the surfaces Tracks D (reporting, P13–P17)
and F (scale/lifecycle, P25) added — against a **synthetic, known-count** LEA/FIU scenario. The six
real-world golden cases (Colonial, Ronin, Bitfinex, Hydra/Garantex, CoinJoin, Genesis) assert BIH's
*invariant-honoring behavior against messy real data*; they deliberately do NOT pin exact counts or the
report's court-ready sections (real data drifts). This case is the complement: a controlled fixture whose
exact counts and deterministic report let us assert the **deliverable** an investigator hands a court —
chain-of-custody, methodology, numbered exhibits — plus the LEA-scale payload controls and the case
lifecycle (idempotent re-ingest + export round-trip).

## Why synthetic (and why that is the honest choice here)

The data is **illustrative and NOT real** — the addresses, the "Acme Exchange" label, and the sanctioned-
mixer designation make **no claim about any real person or entity**. The point of this case is the
*machinery*, not a real seizure, and pinning exact counts requires a fixture we fully control. It is built
only from a **public structured-import format** (the Etherscan UI "Download CSV Export" — the P22 importer)
plus **investigator constructions** (trace / entity / exhibit / finding / annotation). No API cassette is
fabricated and nothing is scraped (Invariants #1, #3). A real designation would come from the free OFAC
pillar (see the Ronin case); here the sanctioned risk is a clearly-labelled synthetic claim.

## Scenario — "Operation Ledger"

A subject **S** (`0x5290…9EE7`) is investigated for laundering stolen funds:

- **Confirmed (final) fact** — a 5 ETH theft inflow **V → S** (`0x8617…070D` → S), as pulled via the
  Etherscan API (finalized, high confirmations). This gives the case final facts (the CSV export below
  carries no confirmations, so its rows are `provisional` per Invariant #6).
- **Imported history (Etherscan CSV export, provisional)** — S's normal-transaction export
  (`backend/tests/fixtures/validation/lea_fiu_etherscan.csv`), 5 rows:
  1. **10 ETH IN** from the victim V;
  2. **4 ETH OUT** to an exchange deposit **X** (`0xd8dA…6045`);
  3. **3 ETH OUT** to a (synthetic) sanctioned mixer **M** (`0xdAC1…1ec7`);
  4. a **reverted** 1 ETH OUT (`ErrCode=Reverted`) → a transaction row, **no transfer** (never fabricate a
     movement for a failed tx);
  5. a **zero-value** contract `Approve` → a transaction row, **no transfer**.
- **Claims** — an `attribution` (X = "Acme Exchange", exchange), a sanctioned `risk_assessment` on M, a
  `valuation` on the confirmed theft transfer (5 ETH × $2,500 = $12,500), and an `entity` "Acme Exchange"
  with a membership on X.
- **Investigator work** — a `trace` over S's two outbound transfers, a screenshot `exhibit`, a `finding`
  ("Subject moved 3 ETH to a sanctioned mixer") citing the mixer address + the exhibit, and an `annotation`
  on the trace.

## What a correct BIH run must reproduce (expected counts + court-readiness)

Harness: `backend/tests/integration/test_lea_fiu_validation.py` (a permanent `make smoke` guard, 4 tests).

1. **Deterministic rebuild — exact counts.** Import result `{transactions: 5, transfers: 3, failed: 1,
   skipped: 1}`. Case totals: **6** transactions (5 CSV + 1 confirmed theft), **4** transfers (3 CSV + 1),
   1 attribution, 1 risk_assessment, 1 valuation, 1 entity + 1 membership, 1 exhibit, 1 finding, 1
   annotation. Finality: **1 final** (the theft) + **5 provisional** (the CSV rows). Every invariant audit
   passes.
2. **Court-ready report.** The report renders **Chain of custody** (P2 — every `source_query` listed),
   **Methodology** (P13 — how to read it + the per-chain finality thresholds actually used), and a **List of
   Exhibits** with the screenshot numbered **"Exhibit 1"** (P15). A fixed `generated_at` yields an
   identical `content_hash` across two renders (report determinism — the golden property P13–P17 preserve).
3. **Graph scope / pagination (P25).** `bound_subgraph` reports honest truncation `meta`
   (`total_nodes`/`returned_nodes`/`truncated`); an `?address_id`-equivalent `focus_incident` build returns
   the subject's neighborhood. This is the LEA-scale payload control (a large case need not ship whole).
4. **Idempotent re-ingest (Invariant #7).** Re-importing the identical export adds **zero** new tx/transfer
   rows (content+`occurrence` dedup); audits stay green.
5. **Export round-trip.** The case exports to a self-contained `.casefile` and re-verifies (`verify_casefile`
   → `ok`, `audits_passed`), and the **P27 in-DB `audit_baseline` anchor travels** inside `case.db`
   (`final_anchor_present`), so the immutability baseline is tamper-evident in the bundle.

## Caveats / what "correct" looks like

- **Provisional CSV facts are correct, not a defect.** An Etherscan CSV export has no confirmations column,
  so BIH ingests its rows as `provisional` (Invariant #6) — they may be corrected on a later API re-fetch.
  The case carries one genuinely-final fact (the separately-confirmed theft) so the immutability baseline is
  non-empty and its P27 anchor is exercised on export.
- **The sanctioned label is synthetic.** In a real case this comes from the free OFAC pillar; here it is a
  labelled synthetic claim so the report's sanctioned-risk + glossary surfaces are exercised without
  asserting a real designation.

## Results — actual vs. expected (built in BIH 2026-07-04)

**Outcome: PASS.** The case rebuilds to the exact counts above; the report carries chain-of-custody +
methodology + a numbered exhibit and renders deterministically; `bound_subgraph`/`focus_incident` bound the
payload; a second identical import is a zero-dupe no-op; and the `.casefile` round-trips with the P27 anchor
intact. No fabrication, no lost provenance, no invariant violation.

| # | Acceptance | Result |
|---|---|---|
| 1 | Rebuilds deterministically with expected counts | ✅ PASS — 6 tx / 4 transfers / 1 each claim+exhibit+finding+annotation; 1 final + 5 provisional |
| 2 | `make audit` + `make smoke` pass on it | ✅ PASS — 11/11 audits; the 4 harness tests are `@pytest.mark.smoke` |
| 3 | Report shows chain-of-custody + methodology + numbered exhibits | ✅ PASS — all three sections present; "Exhibit 1"; deterministic `content_hash` |
| 4 | Export round-trips (export → re-import → audit) | ✅ PASS — `verify_casefile` ok + audits pass + `final_anchor_present` (P27) |
| 5 | 2nd ingest = zero dupes | ✅ PASS — re-import adds 0 tx / 0 transfers; audits stay green |

## Notes

- This case pairs with the real-world golden cases: they prove BIH behaves correctly on real, messy data;
  this proves the court-ready deliverable + lifecycle machinery is exact, deterministic, and reproducible.
- To extend it into a multi-report or superseding-report scenario, generate a second report with
  `supersedes_report_id` — out of scope for this deterministic guard.
