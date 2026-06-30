# Validation case — Genesis / high-fan-in **scale stress** · BTC + EVM

**Purpose:** the **scale** validation. Where the other cases verify correctness of a flow, this one
verifies the tool stays **legible and investigation-useful on a dense case** — a high-degree address with
a huge dust fan-in and a long tail of unpriced value. It is the stress case for the focused/aggregated
view, the valuation surfacing, and the scale-aware honesty banner.

## The stress data (`cases/live`, "BTC genesis + DeFiLlama")

- **Bitcoin genesis address `1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa`** — Satoshi's coinbase address, famous for
  receiving **tens of thousands of tiny "tribute" dust inbounds** over the years. It is the **named** scale
  stress address. (In this *bounded* ingest the genesis carries only a sample of that history — the
  hairball it stands for is what the view must survive at full scale.)
- **EVM `0xd8da6bf2…6045` (vitalik.eth)** — the densest node *actually* present in this bounded ingest:
  **degree ~2,362 (≈1,669 inbound)**, the inbound side dominated by **worthless airdropped ERC-20 tokens
  with no DeFiLlama price** — i.e. real dust fan-in with a long unpriced tail. This is the working stress
  node the view is tuned against.

## What "renders usefully" means here (acceptance)

1. **No hairball by default.** `/api/view` opens on the **seed/anchor** (the genesis) and walks **1 hop**,
   capped at **~150 nodes**; a high-degree node never auto-renders its full neighbourhood. The genesis
   default view is a handful of nodes: the genesis + its significant txs + **one "N inflows · $X · dust"
   aggregate**. Vitalik focused = ~60 significant counterparties + an inbound/outbound dust aggregate
   (bounded from 2,362), navigable, not a 2,362-edge hairball.
2. **Dust / high fan-in collapses.** Small/unflagged counterparties (below a USD floor, not risk/attributed)
   collapse into **one summary node + edge per direction**, labeled with count + USD total + a no-price
   count. The aggregate is **display-only** (Inv #5 — no fact/edge is written) and **expandable to the real
   underlying** (its `underlying` lists the real counterparties, each keeping its own provenance — Inv #3).
3. **Valuation is surfaced (the DeFiLlama payoff).** Every priced edge shows **USD value-at-time** and edge
   width scales by USD (the honest cross-asset comparator); the selected/anchor node shows a **received /
   sent** value summary (native + USD-at-time). **Missing prices are an honest gap** — the edge stays
   visible with its native amount, de-emphasised, **never shown as $0** (Inv: sourced value, never
   fabricated). In this case **~37% of movements are priced** (ETH/BTC/majors); the airdrop tail is gaps.
4. **Scale-aware honesty.** Any partial view shows **"displaying N of M (bounded)"**, mirroring the
   facts-honesty principle. A **search/center box** jumps to a specific address in a large graph; a
   per-node **"focus / expand here"** re-centers; clicking a dust aggregate expands it.
5. **Filters re-run the focused view**, not just hide/show: **group-dust toggle**, **value floor (min $)**,
   **flagged-only**, **hops**. A **ranked counterparties** list (top by USD, flagged surfaced) sits beside
   the graph for the selected node.

## Invariants exercised

- **Inv #5 (display-only aggregates):** a regression test snapshots every table's row count, runs
  `build_view` with many param combinations, and asserts **not one row changed** + audits stay green.
- **Inv #3 (provenance preserved):** aggregates are expandable to the real underlying counterparties /
  movements, which carry their own `source_query`.
- **Honest valuation:** no price → no value (gap), never `$0`; multi-source prices are flagged
  `value_contested` (the claims stay side-by-side, the edge shows one representative figure for width).

## Known gap

The named genesis hairball (tens of thousands of dust inbounds) is **not fully ingested** in `cases/live`
(it is a bounded sample). The architecture handles it — the dense EVM node (vitalik) demonstrates the
fan-in collapse at real scale — but a full-genesis ingest is a future fixture. See [PROGRESS.md] and the
`test_graph_view.py` suite.
