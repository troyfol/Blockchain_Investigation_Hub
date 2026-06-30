# Validation case — CoinJoin detection + honest tracing boundary · BTC

**Purpose:** validate the **CoinJoin detection** algorithm on a real CoinJoin transaction, and confirm
the trace treats a CoinJoin as a **deconfusion boundary** (the BTC analogue of the Tornado Cash honest
gap) — never fabricating a 1:1 input→output link through it. Bitfinex tested the *negative* (its
consolidation was "not CoinJoin-flagged"); this tests the **positive** detection path. **Prompt order:
SECOND**, after the Hydra/Garantex attribution case.

## Ground truth / context

CoinJoins (Wasabi, Samourai **Whirlpool**, JoinMarket) combine many participants' inputs into one
transaction with **many equal-value outputs**, deliberately breaking the heuristic that a tx's inputs and
outputs belong to one entity. They are a recognized laundering / privacy mechanism and a hard tracing
boundary — both Samourai Whirlpool (founders **DOJ-charged April 2024** for money laundering) and the
Wasabi coordinator (shut down 2024) are real LE-relevant examples. Detection is **structural**, so any
genuine CoinJoin transaction is a valid fixture.

## STEP 0 — pick a real CoinJoin tx (do this first; confirm by structure, do not guess)

Identify a **real CoinJoin transaction** and confirm it via Esplora by its structure — the signature is
**many inputs and many equal-value outputs** (e.g. Samourai Whirlpool pools: 5 inputs / 5 outputs of
exactly 0.01 / 0.05 / 0.5 BTC; or a Wasabi coordinator coinjoin with a large cluster of ~0.1 BTC equal
outputs). Record that tx (and enough of its input/output ancestry to trace into and out of it) as the
bounded Esplora fixture. Use a txid you have **verified** has the CoinJoin structure — not a guessed one.

## What a correct BIH run must reproduce (comparison checklist)

1. **Facts:** the CoinJoin tx ingests as `tx_input`/`tx_output` rows (Inv #5), provenance on each (Inv #3).
   No synthesized transfer.
2. **CoinJoin detection — the headline:** BIH's CoinJoin-detection (`docs/algorithms.md`) **flags the tx
   as a CoinJoin** from its structural signature (many inputs, many equal-value outputs). Assert the flag
   is set on the real tx — and, as a negative control, that an ordinary (non-CoinJoin) tx in the fixture
   is **not** flagged.
3. **Tracing honest-gap:** a FIFO/trace through the CoinJoin must **not** assert a deterministic 1:1
   input→output link across the mix — it treats the CoinJoin as a **deconfusion boundary** (flags the
   ambiguity / stops, like the Tornado Cash gap) and never fabricates a through-link. This is the BTC
   mixer-honesty analogue.
4. **Attribution:** the CoinJoin coordinator (Wasabi/Samourai) may carry a GraphSense tag; if so it
   surfaces, else **gracefully absent** — never fabricated (Inv #4).
5. **Report/export:** reproducible `.casefile` with the CoinJoin flag + the bounded-trace ambiguity cited.

## Caveats / what "correct" looks like

- The point is **honest ambiguity**: BIH should *recognize* the CoinJoin and *refuse* to assert a
  false through-link — recognizing-and-flagging is success, a fabricated link is the failure to catch.
- Keep the fixture bounded (the one CoinJoin tx + a little ancestry), deterministic, replayed offline.
- **Find-the-gaps, not pass-the-test:** if detection misses a real CoinJoin, false-flags an ordinary tx,
  or the trace fabricates a through-link, record it in `PROGRESS.md` + a Results section here — do not
  tune assertions.

## Results — actual vs. expected (recreated in BIH 2026-06-29)

Harness: `backend/tests/integration/test_coinjoin_detection_validation.py` (a permanent `make smoke` guard).
Fixtures (`backend/tests/fixtures/validation/coinjoin_*`): RAW keyless Blockstream Esplora responses
recorded once, replayed offline. **Outcome: BIH detects the real CoinJoin from its structure, flags the
co-spend cluster over it as untrustworthy, and the trace never asserts a deterministic/factual through-link
across the mix — it reports un-ingested ancestry as unresolved and labels every apportionment as a
convention.** No BIH defect found; the one divergence (the boundary is flag-based, not an auto-halt) is a
documented characterization of the implemented behavior, not a tuned assertion.

**STEP 0 — CoinJoin confirmed STRUCTURALLY via Esplora (not guessed):**

- **CoinJoin `323df21f0b0756f98336437aa3d2fb87e02b59f1946b714a7b09df04d429dec2`** — a real Samourai
  Whirlpool 0.05 BTC pool tx: **5 inputs / 5 outputs all exactly 5,000,000 sat**, 5 distinct input
  addresses. Hits `is_probable_coinjoin` via BOTH paths (≥5 inputs AND ≥5 equal outputs; and ≥5 outputs at
  a known Whirlpool denomination).
- **Tx0 `333f45431e47b9543772013ac83a9b33cc58dc3245ccfd48b972107bb8405c13`** — the Whirlpool Tx0 funding
  tx: 1 input / 19 outputs (off-denom 5,010,000 premix). A real ORDINARY tx (negative control) **and** a
  direct parent of the CoinJoin (a CJ input spends `Tx0:8`), supplying FIFO ancestry into the mix.

The candidate txids came from a public Whirlpool walkthrough; the **structure was verified on-chain via
Esplora before recording** (5×5 equal at the 0.05-BTC denom; Tx0 single-input/off-denom).

| # | Checklist item | Result | Actual vs. expected (the gaps) |
|---|---|---|---|
| 1 | **Facts** as `tx_input`/`tx_output`, never a transfer | ✅ PASS | The 5 equal 0.05-BTC outputs + 5 inputs ingest as UTXO facts; **zero `transfer` rows** (Inv #5), all `v_value_movement` UTXO `src` NULL, provenance on every row (Inv #3). |
| 2 | **CoinJoin detection — the headline** | ✅ PASS | `is_probable_coinjoin` is **True** for the Whirlpool tx and **False** for the ordinary Tx0 (negative control). `cluster_cospend` forms **1 cluster** over the 5 mix participants and flags all **5 memberships `possible-coinjoin` with reduced confidence 0.5** — the flagged address set is **exactly** the CoinJoin's 5 input addresses; the Tx0 input address is not flagged (a single-input tx never forms a co-spend cluster). The flag is the honest hedge: co-spend over a CoinJoin is recorded *with a marker that says do not trust it*. |
| 3 | **Tracing — honest deconfusion boundary** | ✅ PASS (+ documented gap) | FIFO-tracing INTO the mix wrote **2 `basis='fifo'` links** (from the one input whose Tx0 ancestry is in-DB) and reported **7 unresolved** (the other inputs' ancestry is not in-DB — reported, **never guessed**). Every written link is an explicitly **labeled convention** (`is_convention=True`, `confidence=None`) — BIH asserts **no** deterministic/confident through-link and **no** investigator-deterministic link; and no through-link exists as a fact (Inv #5). The single resolved input **fans across two outputs** (5,000,000 + 10,000 sat), so even the FIFO convention is visibly **not** a 1:1 recovery. **Find-the-gap:** BIH's deconfusion boundary is **flag-based** (the `possible-coinjoin` membership flag + the `is_convention`/`confidence=None` labeling), **not an automatic trace halt** — `fifo_trace_transaction` does not itself consult `is_probable_coinjoin` to stop; it apportions across the mix as a labeled non-fact. Recognizing-and-flagging is implemented; an auto-stop at the boundary is not. The honesty that holds: the mix is recognized + flagged, the apportionment is never a fact or a confident claim, and un-ingested ancestry is reported, not fabricated. |
| 4 | **Attribution** | ✅ PASS (honest absence) | No public GraphSense TagPack was ingested for the Whirlpool/Samourai coordinator addresses, so attribution is **gracefully ABSENT** — BIH invents no label (Inv #4). |
| 5 | **Report / export** | ✅ PASS | Audits 10/10 (incl. `no-fabricated-utxo-edge`, `entity-resolution-sanity`); a reproducible `.casefile` exports and re-verifies — carrying the `possible-coinjoin` flag + the convention-labeled trace links (the cited ambiguity). |

**Verdict:** the CoinJoin detection validation passes — BIH detects the real Whirlpool tx structurally
(while correctly NOT flagging the ordinary Tx0), marks the co-spend cluster over it as low-confidence
`possible-coinjoin`, and refuses to assert any deterministic or factual 1:1 through-link across the mix
(FIFO links are explicit conventions; un-ingested ancestry is reported unresolved). The one honest nuance —
the boundary is enforced by *flagging*, not an automatic FIFO halt — is documented above, not tuned away.

## Next step

After this verifies, all the major gaps (positive attribution, multi-source, CoinJoin detection) are
closed → proceed to visual tuning + executable packaging (the executable being gated on validation per
the agreed sequence).

## Sources
- GraphSense algorithms / CoinJoin detection: `docs/algorithms.md` (in-repo)
- Samourai Whirlpool DOJ charges (Apr 2024): https://www.justice.gov/usao-sdny/pr/founders-and-ceo-cryptocurrency-mixing-service-arrested-and-charged-money-laundering
- CoinJoin overview: https://en.bitcoin.it/wiki/CoinJoin
