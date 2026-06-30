# Validation case — Hydra / Garantex: positive attribution + multi-source · BTC+EVM

**Purpose:** close the **biggest validation blind spot** — every prior case (Ronin, Colonial, Bitfinex)
had GraphSense attribution **gracefully ABSENT**. This case is the first to exercise the **positive
attribution path** (a real public GraphSense TagPack resolving an entity) **and** the **multi-source,
never-merge model (Invariant #4)** — the project's core differentiator. Anchor on an address that is in
**both** the public GraphSense TagPacks **and** the OFAC SDN list, so two independent sources speak about
the same address, side-by-side. **Prompt order: this case FIRST**, then the CoinJoin case.

## Ground truth (public sources)

- **2022-04-05:** OFAC sanctioned **Hydra** (Russia-based darknet market — the largest ever) and
  **Garantex** (Russian crypto exchange). The action added **117 Hydra** crypto addresses and **3
  Garantex** addresses to the SDN List. Both were also subjects of real LE action (Hydra was seized by
  US/German law enforcement, April 2022).

## Confirmed OFAC addresses (from the SDN designation)

- **Garantex — ETH:** `0x7FF9cFad3877F21d41Da833E2F775dB0569eE3D9`
- **Garantex — BTC:** `3Lpoy53K625zVeE47ZasiG5jGkAxJ27kh1`
- **Garantex — (listed under USDT, BTC-format):** `3E6ZCKRrsdPc35chA9Eftp1h3DLW18NFNV`
- **Hydra — 117 BTC addresses** in the same designation (use one as the anchor if it's also in the
  TagPacks; darknet-market addresses are more likely to carry an *independent* GraphSense entity tag than
  an exchange's).

## STEP 0 — pick the dual-listed anchor (do this first)

Clone/read the public GraphSense TagPack repo (`github.com/graphsense/graphsense-tagpacks`, `packs/`) and
find an address that is present **both** as a GraphSense tag (entity = Hydra / Garantex / a darknet
market) **and** in the OFAC SDN list (one of the addresses above, or a Hydra BTC address). Use that exact
address as the anchor. **Prefer an address whose GraphSense tag comes from a *non-OFAC* source** (e.g. a
darknet-market research pack) so the two claims are genuinely independent — not both OFAC-derived. If only
an OFAC-sourced GraphSense tag exists, note that the two claims share a root source and the multi-source
demonstration is weaker (still valid for the positive-attribution path).

## What a correct BIH run must reproduce (comparison checklist)

1. **Facts:** ingest the anchor's transactions (Esplora for BTC / Etherscan for the Garantex ETH address)
   as `tx_input`/`tx_output` (BTC) or `transfer` (EVM), with provenance (Inv #3, #5).
2. **Attribution — the headline (first POSITIVE validation):** the anchor resolves via the GraphSense
   importer to an `attribution(label=…, source='graphsense')` **and** an `entity` + `entity_membership`
   (e.g. entity = "Hydra Market"). This is the path all prior cases only tested in the negative.
3. **Risk:** BIH's **OFAC connector independently flags the same address** `sanctioned` (it's in the SDN
   set) — `risk_assessment(category='sanctioned', source='ofac-sdn')`. If the GraphSense tag carries an
   `abuse` (darknet_market), that yields a second categorical risk row too.
4. **MULTI-SOURCE, never merged (Invariant #4) — the second headline:** the GraphSense attribution and
   the OFAC risk (and any GraphSense abuse-risk) coexist as **separate, source-stamped claims about the
   same address, side-by-side** — BIH does **not** merge them into one synthesized label/score. If the
   GraphSense entity label and the OFAC entity name differ, **both are kept**, each with its own
   provenance. Assert ≥2 distinct `source` values on the anchor's claims.
5. **Report/export:** a reproducible `.casefile` showing both sources' claims with their provenance.

## Caveats / what "correct" looks like

- This is the case that proves the **sourcing architecture** end-to-end: positive attribution + multiple
  sources side-by-side, never collapsed. A single merged "answer" would be the **failure** to catch.
- **Find-the-gaps, not pass-the-test:** if GraphSense attribution still comes back absent (Step 0 picked a
  non-covered address), or if BIH merges/dedupes across sources, record it in `PROGRESS.md` + a Results
  section here — do not tune assertions.

## Results — actual vs. expected (recreated in BIH 2026-06-29)

Harness: `backend/tests/integration/test_hydra_garantex_validation.py` (a permanent `make smoke` guard).
Fixtures (`backend/tests/fixtures/validation/hydra_*`): RAW keyless Blockstream Esplora responses (facts),
a **bounded slice of the REAL public GraphSense TagPack/ActorPack** (`packs/hydra.yaml`,
`actors/graphsense.actorpack.yaml`, header/actor verbatim), and a small OFAC SDN snapshot of the verified
HYDRA MARKET designation. **Outcome: BIH drives the POSITIVE attribution path for the first time AND
proves the multi-source, never-merge model — two independent sources speak about the same address,
side-by-side, uncollapsed.** No BIH defect found; the divergences below are about the case's real data
shape, not BIH behavior.

**STEP 0 — dual-listed anchor confirmed empirically (not guessed): `16ZSAEfYpPCj3D94fsNt2okYj9Ue8mxy6T`.**
Cloned the public GraphSense TagPacks repo and intersected `packs/hydra.yaml`'s 117 addresses with the
authoritative machine-readable OFAC SDN crypto-address extract (0xB10C, cited in Sources): **117/117 of the
hydra.yaml addresses are in the OFAC SDN XBT list** — every Hydra address is genuinely dual-listed (the 3
Garantex anchors confirmed too). The chosen anchor is a Hydra Market deposit address with a clean bounded
2-tx history (0.0115 BTC deposit `e5015b6e` h=644385 → forwarded `c41de249` h=645827), so the cassette is
~4 KB. The OFAC SDN entry name/type/program ("HYDRA MARKET" / Entity / CYBER2) were verified against the
OFAC Sanctions List Search (Details.aspx?id=36216) before recording.

| # | Checklist item | Result | Actual vs. expected (the gaps) |
|---|---|---|---|
| 1 | **Facts** as `tx_input`/`tx_output`, never a transfer | ✅ PASS | The 0.0115 BTC deposit ingests as a `tx_output` fact; **zero `transfer` rows** (Inv #5), all UTXO `src` NULL, provenance on every row (Inv #3). **Find-the-gap (live state):** the deposit did not sit — it was **forwarded into Hydra's consolidation** in the same history; the output is `spent=1` and the anchor balance is **0**. BIH shows the funds moved on (as in Colonial/Bitfinex), not a held balance. |
| 2 | **Attribution — the headline (first POSITIVE validation)** | ✅ PASS | The GraphSense importer resolves the anchor to `attribution(label="Hydra Market", category="market", source="graphsense", confidence=0.60)` **and** an `entity("Hydra Market", origin="source", external_id="hydramarket")` + `entity_membership(method="tagpack-actor", flags="cluster-definer")`. **This is the path all three prior cases tested only in the negative** (attribution absent). The entity is `origin='source'` — a real sourced identity, not fabricated. |
| 3 | **Risk** — OFAC independently flags the same address | ✅ PASS (+ gap) | BIH's OFAC connector flags the SAME anchor `risk_assessment(category="sanctioned", source="ofac-sdn")`, rationale `"OFAC SDN: HYDRA MARKET (CYBER2)"`, score NULL (categorical, never synthesized). **Find-the-gap:** the real `hydra.yaml` carries **no `abuse` type**, so the GraphSense side yields **no** categorical risk row — risk on the anchor is **single-source (OFAC)**. The spec's "if the GraphSense tag carries an abuse → a second risk row" is conditional; the real pack is attribution-only, so BIH writes no abuse-risk. We assert that honestly rather than fabricate an `abuse` field to manufacture a second risk source. |
| 4 | **Multi-source, never merged (Invariant #4) — the second headline** | ✅ PASS | The anchor's attribution claims span **2 distinct sources `{graphsense, ofac-sdn}`**, stored side-by-side, each with its own `source_query`. The labels **differ** — GraphSense `"Hydra Market"` (category `market`) vs OFAC `"HYDRA MARKET"` (category `sanctioned_entity`) — and **both are retained**, not collapsed to one canonical label. `claims_display.address_claims` surfaces both sources with **no `combined`/`averaged`/synthesized key**; no synthetic source value exists anywhere; the `append-only-claims` audit confirms no claim was rewritten/merged. |
| 5 | **Report / export** | ✅ PASS | Audits 10/10 (incl. `no-fabricated-utxo-edge`, `append-only-claims`, `entity-resolution-sanity`); every claim across both pillars carries provenance; a reproducible `.casefile` exports and re-verifies. |

**The independence caveat (honest, documented — the one place the demo is weaker than ideal):** the spec
*prefers* an anchor whose GraphSense tag comes from a **non-OFAC source** so the two claims are genuinely
independent. In the real TagPacks, the only packs covering Hydra/Garantex are (a) `hydra.yaml` — authored
by the GraphSense Core Team but whose `source:` backlink **is** the OFAC Treasury action page, and (b) the
generic `ofac.yaml` (which only labels them "Asset listed under US Treasury OFAC Sanctions List", redundant
with the SDN). So the chosen GraphSense claim and the OFAC claim **share a root event** (the 2022-04-05
designation) — the provenance independence is partial. **What still holds robustly:** they are two
**distinct connectors** producing **distinct representations** — GraphSense contributes a named darknet-
market *entity* ("Hydra Market", category `market`, cluster-definer, resolving to an ActorPack actor),
which OFAC's categorical "sanctioned" designation does not. That semantic divergence, kept side-by-side, is
exactly the Invariant #4 behavior under test. We do **not** over-claim full independence; a genuinely
independent demo would need a darknet-research TagPack (non-OFAC backlink) that overlaps the SDN — none
exists in the public repo for these addresses today.

**Verdict:** the positive-attribution + multi-source validation passes — GraphSense resolves the anchor to a
real entity + attribution (the path prior cases only tested in the negative), OFAC independently flags the
same address sanctioned, the two sources' differing labels coexist side-by-side with their own provenance
(never merged), facts stay UTXO-only, and the export spine holds. The one honest weakness (shared OFAC root
source) is documented, not tuned away.

## Next step

After this verifies, prompt the CoinJoin case (`docs/validation/coinjoin_detection_case.md`). Together
they close the positive-attribution, multi-source, and CoinJoin-detection gaps before visual tuning +
executable packaging.

## Sources
- OFAC 2022-04-05 designation (Hydra + Garantex): https://ofac.treasury.gov/recent-actions/20220405
- Chainalysis — OFAC sanctions / Hydra + Garantex: https://www.chainalysis.com/blog/ofac-sanctions/
- GraphSense public TagPacks: https://github.com/graphsense/graphsense-tagpacks
- OFAC sanctioned-address extractor (cross-ref): https://github.com/0xB10C/ofac-sanctioned-digital-currency-addresses
