# Validation case — Ronin Bridge hack (2022, Lazarus/DPRK) · EVM

**Purpose:** a real, LEA/FIU-validated on-chain case to **recreate end-to-end in BIH and compare** BIH's
output against the published investigation + the official OFAC designation. This is the EVM half of the
verification (the BTC half is queued — Colonial Pipeline, see `docs/validation/`). It is the golden
real-world smoketest of the investigation surface (facts → attribution/risk → tracing → report).

## Ground truth (public sources)

- **Theft:** 2022-03-23 (announced 03-29). **173,600 ETH + 25.5M USDC** stolen from the Ronin bridge
  (~$540M at the time) by compromising 5 of 9 validator keys (social engineering). Second-largest crypto
  theft at the time.
- **Attribution:** US Treasury attributed it to **Lazarus Group** (North Korea/DPRK).
- **OFAC designation:** **2022-04-14** — OFAC added the thief's Ethereum address to the SDN list, owner
  listed as Lazarus Group (DPRK cyber program). **This is the anchor and the key validation hook.**
- **Laundering pattern (Elliptic, as of 2022-04-14):** stolen **USDC swapped to ETH via DEXs** (to dodge
  stablecoin freezes/KYC) → **~$16.7M ETH through 3 centralized exchanges** → strategy switched to
  **Tornado Cash** (~$80.3M ETH mixed) → ~$9.7M staged in intermediary wallets → **~$433M still held in
  the attacker's original wallet** at that date.
- **Outcome:** OFAC sanctions (2022-04-14); later (Sept 2022) ~$30M of the funds seized with the help of
  blockchain analytics + law enforcement.

## Anchor (the one address everything hangs off)

- **Ronin Bridge Exploiter / Lazarus (ETH):** `0x098B716B8Aaf21512996dC57EB0615e2383E2f96`
  — OFAC SDN, designated 2022-04-14, owner = Lazarus Group.

Downstream entities (Ronin bridge contract, the 3 exchanges, Tornado Cash router, intermediary wallets)
are **discovered by BIH** from the anchor's transfers — do NOT hand-enter a guessed address list; let the
ingest + trace surface them and confirm against Etherscan. (Tornado Cash addresses are themselves in the
OFAC SDN list from 2022-08-08, so BIH's OFAC connector should independently flag the mixer leg too.)

## What a correct BIH run must reproduce (comparison checklist)

1. **Facts (Etherscan/EVM connector):** ingest the anchor's ETH transfers → inbound ~173,600 ETH from the
   Ronin bridge; outbound flows to DEXs, exchanges, and Tornado Cash. Stored as `transfer` facts with
   provenance (Inv #3). Large ETH balance remains (~$433M-era) — the un-laundered remainder.
2. **Risk — the headline validation (free OFAC connector):** the anchor is flagged
   `risk_assessment(category='sanctioned', source='ofac-sdn')` with rationale naming **Lazarus Group**
   (designated 2022-04-14). BIH's *free* pillar independently reproduces the real Treasury action. The
   Tornado Cash leg should likewise flag sanctioned (SDN 2022-08-08).
3. **Attribution (free GraphSense + optional paid Arkham):** the anchor resolves to an
   entity/label like "Ronin Bridge Exploiter" / Lazarus; exchange-deposit and Tornado Cash counterparties
   carry labels. Stored raw per source, side-by-side, never merged (Inv #4).
4. **Tracing + honest gaps:** forward-trace from the anchor → terminates honestly at **Tornado Cash** (a
   mixer — no fabricated input→output linkage past it) and at **centralized-exchange deposits**
   (attribution boundary). The ~$433M held remainder is visible as un-moved funds. The trace must NOT
   invent flow through the mixer.
5. **Report/export:** a reproducible case file with every fact/claim carrying its `source_query`
   provenance and the sanctions designation cited.

## Caveats / what "correct" looks like

- **Tornado Cash deliberately breaks deterministic tracing** — BIH stopping at the mixer (and flagging it)
  is the *correct* behavior, not a failure. This validates the "honest gaps, never fabricate" invariants.
- **USDC→ETH DEX swaps**: the asset changes; BIH records the transfers as facts but should not synthesize
  a cross-asset "same value" link as a fact (that belongs in a trace as a labeled claim).
- This case validates: EVM facts ingest, the **free OFAC + GraphSense pillars against a real designation**,
  mixer/exchange honest-gap handling, and the report/provenance spine.

## Results — actual vs. expected (recreated in BIH 2026-06-28)

Harness: `backend/tests/integration/test_ronin_lazarus_validation.py` (a permanent `make smoke` guard).
Fixtures (`backend/tests/fixtures/validation/ronin_*`): RAW Etherscan responses recorded ONCE under
RUN_LIVE for the anchor's theft→designation window (blocks 14.40M–14.70M, single page, sort=asc), replayed
offline; `ronin_ofac_sdn.xml` is a small real-designation SDN snapshot. **Outcome: BIH faithfully
reproduces the case — facts, the free OFAC pillar independently re-deriving the Treasury designation, and
the honest-gap invariants all hold. No fabrication, no lost provenance.** The divergences below are
characteristics of the bounded real data, not BIH defects — and one genuine export-robustness gap was
found *and fixed*.

| # | Checklist item | Result | Actual vs. expected (the gaps) |
|---|---|---|---|
| 1 | **Facts** ingested with provenance | ✅ PASS | The **173,600 ETH** theft arrives as an **internal** tx (the Ronin-bridge withdrawal), NOT a top-level `txlist` value transfer — BIH ingests it correctly as an `internal` transfer fact. **25.5M USDC** inbound matches. Every fact carries a `source_query` (Inv #3). |
| 1b | Un-laundered remainder visible | ✅ PASS (nuance) | The "~$433M held" was **point-in-time (2022-04-14)**; the anchor's **current** balance is ~**101.8 ETH** (most has since been laundered). BIH shows live state — the test asserts a remainder exists, NOT a $ figure (asserting $433M would fabricate a stale snapshot). |
| 2 | **Risk** — anchor flagged `sanctioned` (Lazarus) via the FREE OFAC pillar | ✅ PASS | `risk_assessment(category='sanctioned', source='ofac-sdn')`, rationale `"OFAC SDN: LAZARUS GROUP (DPRK2, CYBER2)"`, `score=NULL` (categorical). BIH's free pillar independently reproduces the real Treasury action. The **Tornado Cash** leg flags too (`"OFAC SDN: TORNADO CASH (CYBER2)"`). |
| 3 | **Attribution** | ✅ PASS (honest absence) | OFAC supplies an authoritative `attribution(sanctioned_entity, label='LAZARUS GROUP')`. **No public GraphSense TagPack** was confirmed to cover the anchor, so GraphSense attribution is **gracefully ABSENT** — BIH does NOT invent a label (never fabricate, Inv #4). |
| 4 | **Tracing + honest gaps** — terminate at the mixer, no fabricated flow | ✅ PASS (KEY find-the-gap) | The anchor's 23 direct outbound ETH destinations are large *varied* amounts = **intermediary/consolidation wallets + DEXs**, NOT Tornado Cash (TC pools accept only fixed 0.1/1/10/100 ETH denominations). So the laundering reaches TC **one+ hops downstream**; a forward trace from the anchor terminates at the intermediaries. BIH has **NO direct anchor→TC transfer** and **does not fabricate one** — the mixer is flagged via OFAC, never via a synthesized link. EVM tracing references only real `transfer` facts (no automated path discovery), so it *structurally cannot* invent mixer pass-through. The USDC→ETH DEX swap is two **distinct single-asset** facts, never a merged "same value" edge. |
| 5 | **Report / export** with provenance | ✅ PASS (+ a real gap fixed) | A reproducible, self-contained `.casefile` exports and re-verifies; every claim carries its `source_query`. **Gap found:** the case DB runs in WAL mode, so exporting while a connection is still OPEN bundled an **incomplete `case.db`** (uncheckpointed writes live only in the un-shipped `-wal`), making re-verification fail with false "deleted" rows. **Fixed:** `export_case` now does a defensive `PRAGMA wal_checkpoint(TRUNCATE)` before hashing/zipping (regression-guarded by `test_export_checkpoints_wal_when_db_still_open`). |

**Other real-data observations (not failures):**
- **Dust truncation.** The unfiltered `tokentx` hit the 1000-row cap, ~**969** of which were spam "CASH"
  airdrops to the sanctioned address; the fixture filters to USDC (the material asset). A sanctioned
  address is flooded with dust — a bounded pull can be dominated by spam, worth flagging to an operator.
- **DEX-swap round-trip is visible:** the anchor sent 25.5M USDC to two swap addresses and received ETH
  back from those same addresses — recorded as separate facts (no cross-asset synthesis).

**Verdict:** the free OFAC + Etherscan pillars reproduce the LEA/FIU-validated designation and facts; the
mixer/exchange honest-gap invariants hold (BIH stops and flags, never fabricates); provenance and the
export spine are intact. The one defect surfaced (WAL export-while-open) is fixed and guarded.

## Next step

Pair with the BTC case (Colonial Pipeline — tractable ransom→seizure flow) for cross-chain coverage. To
extend this case to a multi-hop anchor→intermediary→Tornado-Cash trace, record the intermediary wallets'
transfers too (graph expansion already supports it) — out of scope for this single-anchor golden guard.

## Sources
- OFAC designation (2022-04-14): https://home.treasury.gov/policy-issues/financial-sanctions/recent-actions/20220414
- Elliptic — Lazarus / Ronin attribution + laundering flow: https://www.elliptic.co/blog/540-million-stolen-from-the-ronin-defi-bridge
- Chainalysis — Tornado Cash OFAC designation: https://www.chainalysis.com/blog/tornado-cash-ofac-designation-sanctions/
- Chainalysis — Ronin/DPRK seizure: https://www.chainalysis.com/blog/axie-infinity-ronin-bridge-dprk-hack-seizure/
