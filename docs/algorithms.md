# Algorithms — precise specifications

Self-contained algorithm specs. These need no external sourcing; implement and unit/property-test them as
written. Each notes the phase that builds it and the audit/property test that guards it.

## 1. Address canonicalization (Phase 1)

Goal: one canonical string per real address so `(chain, address)` is a true unique key.

- **EVM:** canonical = **lowercase hex** (`0x` + 40 lowercase hex). Store the source's checksummed form in
  `address_display`. Validate length/charset; reject malformed.
- **Bitcoin:** keep the address **as the source presents it**, but normalize encoding case where the
  encoding defines it: **bech32/bech32m** (`bc1...`) are lowercase-canonical; **base58** (`1...`, `3...`)
  is case-sensitive — do not alter. Distinct encodings of the same key are **distinct addresses** (do not
  attempt to unify; that's a heuristic claim, not a fact).

```
canonical(chain, addr):
  if is_evm(chain):  assert re.fullmatch(r'0x[0-9a-fA-F]{40}', addr); return addr.lower()
  if is_btc(chain):  return addr.lower() if addr.lower().startswith('bc1') else addr
```

Property test: `canonical(canonical(x)) == canonical(x)`; EVM checksum and lowercase map equal.

## 2. Finality computation (Phase 1, updated by connectors)

```
confirmations = max(0, tip_height - block_height + 1)        # 0 if unconfirmed (block_height is None)
threshold     = config.finality_threshold[chain]              # BTC 6; ETH ~64; L2 per-chain (CONFIRM)
finality_status = 'final' if confirmations >= threshold else 'provisional'
```

- Recompute on every fetch; **upsert** the transaction (and refresh children) — allowed while
  `provisional`.
- Once `final`, the row is frozen; the **final-immutability audit** (testing.md #4) fails if a final row
  changes.
- A provisional transaction absent from a fresh fetch (reorged/replaced) → delete it and its children
  (allowed, not yet final).

## 3. Valuation precision (Phase 5)

```
from decimal import Decimal, getcontext, ROUND_HALF_EVEN
getcontext().prec = 38
human_amount = Decimal(amount_base_units) / (Decimal(10) ** asset.decimals)
value = (Decimal(unit_price) * human_amount).quantize(Decimal('1e-18'), rounding=ROUND_HALF_EVEN)
# store unit_price, value as TEXT; confidence from source; price at the movement's BLOCK timestamp
```

Property test: no float drift; `value` recomputes identically; missing price ⇒ no `valuation` row (never
fabricate a zero).

## 4. Bitcoin co-spend clustering (Phase 6)

Heuristic: **all inputs to a common transaction are presumed same-controller.** Computed at ingest over
all stored Bitcoin transactions; the cluster set grows as more txs arrive.

Use **union-find (disjoint-set)** over `address` ids that appear together as inputs:

```
for each bitcoin transaction tx with input addresses A = {a1..an} (skip if |A| <= 1):
    if is_probable_coinjoin(tx):        # see §5 — flag, still union but mark memberships
        flag = 'possible-coinjoin'
    else:
        flag = None
    union all of A together              # disjoint-set
# materialize clusters -> entities:
for each cluster C (size >= 2):
    entity = find-or-create cospend entity for C   # origin='cospend-cluster', name=NULL
    for addr in C:
        upsert entity_membership(entity, addr, source='cospend-heuristic',
                                 method='co-spend', confidence=..., flags=flag)
```

**Cluster evolution / merge (decision #3):** a new tx may bridge two previously separate cospend
entities. When union merges two existing cospend entities E1, E2 → pick a survivor S, set the other's
`entity.merged_into = S` (tombstone). **Do not rewrite memberships**; resolution chases `merged_into`.
Reversible by clearing the pointer.

- Resolution function `resolve(entity)`: follow `merged_into` to the terminal entity; the audit asserts
  no cycle.
- Confidence: co-spend is strong but not certain; set a fixed default (e.g. 0.9) — a config knob — and
  lower it / always flag when CoinJoin is suspected.

Property test: union-find correctness (same cluster ⇔ transitively co-spent); idempotent re-ingest does
not create duplicate memberships (upsert on `(entity_id, address_id, source, method)` — note these
memberships are the one place a controlled upsert is allowed, to keep co-spend deterministic).

## 5. CoinJoin detection (Phase 6) — best-effort flagging

CoinJoin txs break the co-spend assumption (inputs are NOT same-controller), so memberships derived over
them must be **flagged** (`flags='possible-coinjoin'`), not silently trusted. Detection is heuristic and
imperfect (PayJoin is ~invisible) — flagging is a safety signal, not a guarantee.

Heuristics (combine; tune thresholds in config — CONFIRM current patterns at build):

- **Equal-output pattern (Wasabi/Whirlpool):** many outputs share an (almost) identical value; ≥ K
  equal-value outputs (e.g. K≥5) with many inputs ⇒ probable CoinJoin.
- **Whirlpool pool sizes:** outputs clustered at known denominations (e.g. 0.001/0.01/0.05/0.5 BTC) —
  treat as a strong signal.
- **Structural:** #inputs and #outputs both high and roughly balanced, uniform output values, no obvious
  change output.

```
is_probable_coinjoin(tx):
    vals = [o.amount for o in tx.outputs]
    most_common_count = max(Counter(vals).values())
    return (len(tx.inputs) >= K_IN and most_common_count >= K_EQUAL_OUT) \
           or matches_known_pool_denominations(vals)
```

When detected, co-spend memberships over that tx still form (so the investigator sees the structure) but
carry the flag and reduced confidence; the investigator can withdraw a wrong membership via
`entity_membership_retraction` (append-only). Golden smoketest: a known Whirlpool tx ⇒ flagged
memberships.

## 6. FIFO Bitcoin tracing (Phase 8) — labeled apportionment, not path discovery

FIFO is the v1 default Bitcoin tracing **convention** — chosen for legal pedigree (the rule in Clayton's
Case; applied to crypto in *D'Aloia v Persons Unknown*) and reproducible transparency, **not** accuracy.
It is applied **along a path the investigator has already expanded**, not as automated path discovery
(deferred). Output is always rendered as a named convention (`basis='fifo'`), never as ground-truth flow.

Within one transaction, apportion inputs to outputs in order:

```
fifo_apportion(tx):
    # inputs and outputs in ledger order; amounts are base-unit ints
    in_queue  = deque((i.id, int(i.amount)) for i in sorted(tx.inputs, key=input_index))
    out_queue = deque((o.id, int(o.amount)) for o in sorted(tx.outputs, key=output_index))
    links = []                                   # (source_output_id_of_input, dest_output_id, amount)
    cur_in_id, cur_in_amt = in_queue.popleft()
    while out_queue:
        out_id, out_need = out_queue.popleft()
        while out_need > 0:
            take = min(cur_in_amt, out_need)
            links.append((cur_in_id, out_id, take))   # cur_in is a prev tx_output being spent
            cur_in_amt -= take; out_need -= take
            if cur_in_amt == 0 and in_queue:
                cur_in_id, cur_in_amt = in_queue.popleft()
            elif cur_in_amt == 0:
                break                              # inputs exhausted (fee remainder)
    return links
```

- Each link → `trace_btc_link(source_output_id=<the input's prev tx_output>, dest_output_id=<this tx's
  output>, basis='fifo', confidence=...)`. `source_output_id` is the prev-output the input spends (must
  be in-DB; else mark the link unresolved).
- **Fee handling:** inputs − outputs = fee; the remainder after outputs are filled is the fee (not a
  link). Multi-hop = chain these per-transaction apportionments along the expanded path, in `trace`
  `ordering`.
- **Manual override:** investigator can add/replace links with `basis='investigator'`.

Property tests (conservation): sum of link amounts into any output == that output's amount; sum out of any
input ≤ that input's amount; no negative amounts; total linked == total outputs (= inputs − fee).

## 7. Same-address cross-chain heuristic (Phase 6) — weak, low-confidence

Identical hex across EVM chains *usually* implies the same controller but not always, and is meaningless
across the EVM/Bitcoin boundary. Memberships from `method='same-address-heuristic'` carry **low
confidence** and clear labeling, and are never auto-applied across the EVM/BTC boundary.
