# Findings — External-fact confirmation (batched): Esplora, DeFiLlama, finality

**Date:** 2026-06-28
**Scope:** the three CLAUDE.md §6 "volatile — confirm against live docs" items, batched into one pass:
Esplora endpoints/pagination (#4), DeFiLlama coins endpoint + coin-key format (#5), per-chain finality
thresholds incl. bsc (#7). Sources fetched live 2026-06-28 (see each section).

## TL;DR

Esplora is accurate as-built — no code change. DeFiLlama's shape is confirmed but it has **one real gap
that blocks the Arkham bsc work** (no `bsc` native-coin key) plus one thing to verify (bsc token key
casing). Finality thresholds now have **real cited numbers** replacing the placeholders/`TODO: confirm`;
none *must* change (they err conservative-high, which Invariant #6 wants), but bsc's TODO can be closed
and polygon's rationale is stale.

## 1. Esplora (#4) — CONFIRMED, no functional change

Source: Blockstream Esplora `API.md` (raw, fetched 2026-06-28) + live `https://blockstream.info/api`.

- Base `https://blockstream.info/api/` ✓; **amounts always in satoshis** ✓.
- `GET /address/:a/txs` → "up to **50 mempool** transactions plus the first **25 confirmed**", newest
  first. The connector uses this for page 1; mempool txs flow through and must be marked `provisional`
  (confirm `esplora_adapter` does so).
- `GET /address/:a/txs/chain[/:last_seen_txid]` → **25 confirmed per page**, cursor = last txid seen.
  ✓ exactly matches `_collect_txs` (`/address/{a}/txs/chain/{last_confirmed}`, page size 25).
- `GET /blocks/tip/height` → tip height for finality ✓.
- Tx format fields the adapter relies on (`txid`, `vin[].prevout`, `vout[].value` /
  `scriptpubkey_address`, `status.confirmed/block_height/block_hash/block_time`) ✓ present.

Action: none functional. Refresh the `CONFIRMED-AT-BUILD 2026-06-26` docstring → `RE-CONFIRMED
2026-06-28`. Optional hardening: a test asserting the page-1 split (≤50 mempool + 25 confirmed) is
handled and mempool rows land `provisional`.

## 2. DeFiLlama (#5) — shape CONFIRMED; one gap to fix, one to verify

Source: live `https://coins.llama.fi/prices/historical/1700000000/<keys>` fetched 2026-06-28.

- Base + `/prices/historical/{unix_ts}/{coins}` ✓. Response shape
  `{"coins": {"<key>": {"symbol","price","timestamp","confidence"}}}` ✓.
- **`decimals` is NOT returned for `coingecko:` keys** (only sometimes for `{chain}:{contract}`). Code
  reads `coin.get("decimals")` → `None`, which is fine — just note it; don't assume decimals present.
- Live confirmations: `coingecko:ethereum` → ETH `$1986.32` conf `0.99`; `coingecko:binancecoin` → BNB
  `$241.98` conf `0.99`.

**GAP A (real — blocks the Arkham bsc work).** `NATIVE_COINGECKO_ID` is **missing `bsc`**. Now that
Arkham bsc **native BNB** transfers ingest, valuing them calls `coin_key("bsc", <native>)` →
`UpstreamError("no native-coin price key for chain 'bsc'")`, and `supported_chains()` (which returns
`set(NATIVE_COINGECKO_ID)`) excludes bsc entirely. **Fix:** add `"bsc": "binancecoin"` (confirmed live).
This closes the loop between the Arkham bsc fix and the valuation layer.

**GAP B (verify).** A BSC token key `bsc:0x55d3…955` (USDT, **mixed-case**) returned **nothing** in the
live test, while the `coingecko:` keys resolved. Most likely DeFiLlama wants the **lowercased** contract
under the `bsc:` prefix. In production `canonical_address` lowercases EVM contracts, so real keys should
already be lowercase — but add a test that a real **lowercased** bsc token key returns a price, and
confirm DeFiLlama's BNB-chain prefix is `bsc` (not `binance`/`bnb`).

## 3. Finality thresholds (#7) — real numbers confirmed; close the TODO

Current `DEFAULT_FINALITY_THRESHOLDS`: bitcoin 6, ethereum 64, arbitrum 20, optimism 20, base 20,
polygon 128, bsc 15.

- **bitcoin 6** ✓ long-standing convention. **ethereum 64** ✓ (~2 epochs → the consensus `finalized`
  checkpoint).
- **bsc:** BEP-126 fast finality — a block is final once **two continuous blocks are justified**, i.e.
  **~2 blocks** in most cases (recent upgrades put wall-clock finality near 0.65s). The current `15` is
  conservative-safe; **replace the `TODO: confirm`** with this cited value. Keep 15 (conservative) or
  lower toward ~3 — higher is safer per Invariant #6.
- **polygon:** Heimdall v2 (Jul 2025) **milestones** give deterministic finality in **2–5s with reorgs
  ≤2 blocks** — finality no longer waits for an Ethereum checkpoint. The `128` placeholder is now far
  over-conservative (still *safe*, but the "PoS checkpoints are large" rationale is stale). May lower
  (e.g. 16–32); document if so.
- **arbitrum / optimism / base (20):** true finality = **L1 settlement**, not an L2 block count; the
  threshold is a soft policy proxy. Keep conservative; document the simplification (an L2 "finalized"
  flag should ideally key off the L1 batch being finalized, a later enhancement).

**Direction-of-safety:** thresholds intentionally err **high** (Invariant #6 — never freeze tip data as
`final` prematurely). So none *must* drop; only bsc's `TODO` needs closing and the comments need real
citations. Lowering polygon/bsc is a policy choice, not a correctness fix.

## Net code actions (small, low-risk)

1. **DeFiLlama:** add `"bsc": "binancecoin"` to `NATIVE_COINGECKO_ID`; add a test for bsc **native**
   valuation, and a test for a **lowercased** bsc **token** key returning a price (Gap B).
2. **config.py:** update finality comments with the cited real numbers; drop bsc `TODO: confirm`;
   optionally lower `polygon` (and/or `bsc`) as a documented policy decision.
3. **Esplora:** refresh the confirmation date; optional page-1 mempool/confirmed split test.
4. **PROGRESS.md:** mark #4/#5/#7 confirmed against live sources (2026-06-28); record the polygon/bsc
   threshold values as open *policy* knobs, not unknowns.

## Sources

- Esplora API: https://raw.githubusercontent.com/Blockstream/esplora/master/API.md
- DeFiLlama coins (live): https://coins.llama.fi/prices/historical/1700000000/coingecko:ethereum,coingecko:binancecoin
- BSC fast finality (BEP-126): https://github.com/bnb-chain/BEPs/blob/master/BEPs/BEP126.md
- Polygon Heimdall v2 finality: https://docs.polygon.technology/pos/concepts/finality/finality
