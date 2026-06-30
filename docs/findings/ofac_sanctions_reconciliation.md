# Findings — OFAC sanctions → BIH risk (free risk pillar)

**Date:** 2026-06-28
**Method:** docs-sourced (official OFAC + the 0xB10C extractor + Chainalysis free API docs, fetched 2026-06-28).
**Why:** the free **risk** pillar (see `project-bih-sourcing-architecture` memory). Complements GraphSense
`abuse` tags (Phase B of that build) with authoritative sanctions data.
**Target model:** `models/claims.py::RiskAssessment` (`address_id, score, score_scale, category,
rationale, source, retrieved_at`). Sanctions are **categorical** → `score=None`, `score_scale=None`.

## TL;DR

Sanctions screening is free and authoritative. Two sources, both **sanctions-only categorical risk**
(no numeric score): the **OFAC SDN list** (US Treasury, no key) and the **Chainalysis free sanctions
API** (free key). Build OFAC SDN as the primary free pillar (no key, no account); Chainalysis is an
optional side-by-side second source (Invariant #4). Same chain-filter pattern as Arkham/GraphSense
(BIH v1 = BTC + EVM only; skip + report the rest).

## Source 1 — OFAC SDN list (primary, free, no key)

Authoritative file: `sdn_advanced.xml` (~80 MB) from US Treasury
(`treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml`). Crypto addresses appear as
**"Digital Currency Address - <TICKER>"** features attached to SDN entries, so the XML carries, per
address: the **entity name**, the **sanctions program**, and the address — rich provenance.

Convenience mirror: **`0xB10C/ofac-sanctioned-digital-currency-addresses`** (MIT) — its `lists` branch
has **nightly-regenerated** per-asset files `sanctioned_addresses_<TICKER>.txt` (one address/line) and
JSON variants. Covered tickers include: **XBT, ETH, USDC, USDT, ARB, BSC**, ETC, plus XMR/LTC/ZEC/DASH/
BTG/BSV/BCH/XVG/XRP/TRX/SOL. The lists are **addresses only** (no entity/program metadata).

**Recommendation:** parse the **official SDN XML** as the authoritative primary (gives entity + program
→ richer rationale and an optional attribution), with the 0xB10C lists as a documented lightweight
fast-path / cross-check. Both are legitimate structured imports of public data (Invariant #1).

## Source 2 — Chainalysis free sanctions API (optional, free key)

`GET https://public.chainalysis.com/api/v1/address/{address}` with an `X-API-Key` header (key is free
via a request form). Returns `identifications[]`; each entry has `category`, `name`, `description`,
`url`. Empty array ⇒ not sanctioned. Per-address query (not bulk). Use as an API `get_risk` connector,
a second sanctions source stored side-by-side with OFAC (Invariant #4).

## Mapping → BIH

| Source field | BIH target | Notes |
|---|---|---|
| sanctioned address + `<TICKER>` | `Address(chain, address_display)` | ticker→chain map; canonicalize per chain |
| (the fact of being listed) | `RiskAssessment(category='sanctioned', score=None, score_scale=None)` | categorical risk |
| SDN entity name + program / Chainalysis `name`+`description` | `RiskAssessment.rationale` | e.g. "OFAC SDN: <entity> (<program>)" |
| source | `RiskAssessment.source` = `ofac-sdn` / `chainalysis-sanctions` | distinct per source (Invariant #4) |
| SDN entity name (when present) | optional `Attribution(label=entity, category='sanctioned_entity', source='ofac-sdn')` | only when a name is available (XML / Chainalysis), not from the plain lists |

**Ticker→chain + skip rule:** map `XBT→bitcoin`, `ETH→ethereum`, `ARB→arbitrum`, `BSC→bsc`, and
ERC-20 tickers (`USDC`/`USDT`) → `ethereum` (they're EVM addresses). **Skip + report** non-BTC/EVM
assets (XMR, LTC, ZEC, DASH, BTG, BSV, BCH, XVG, XRP, TRX, SOL, and ETC unless you add it) — they'd
raise in `canonical_address`. Same pattern as Arkham tron / GraphSense unsupported currencies.

## Reconciliation points

1. **Sanctions are mutable** — OFAC delists addresses. These are **not immutable facts**; idempotent
   re-ingest must reflect the current list (a delisted address simply isn't in the next fetch). Carry
   `retrieved_at`; don't freeze sanctions as permanent. (Conceptually like `provisional`.)
2. **Categorical only** — `score=None`; never invent a numeric score.
3. **Provenance** — `source_query` per ingest: for the SDN XML, `raw_response` = the file (hashed) +
   the SDN publication date; for the 0xB10C list, the list file; for Chainalysis, the JSON response
   (Invariant #3).
4. **Two sources, never merged** — OFAC and Chainalysis stored side-by-side; they can differ (Inv #4).
5. **ERC-20 token tickers are ethereum addresses** — map to `ethereum`, don't treat the token as a
   distinct chain.

## Suggested phasing

- **A (free pillar, no key):** ingest the **OFAC SDN XML** → `risk_assessment(category='sanctioned')`
  with `rationale` = entity + program; ticker→chain filter + skip/report; idempotent re-ingest;
  per-fetch provenance. (Fast-path option: 0xB10C lists if XML parsing is deferred — addresses only.)
- **B:** emit the optional `attribution(sanctioned_entity)` when the XML/API supplies an entity name.
- **C:** Chainalysis API `get_risk` connector (free key) as a second side-by-side sanctions source.

## Sources

- OFAC SDN (authoritative): https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml
- 0xB10C extractor + nightly lists: https://github.com/0xB10C/ofac-sanctioned-digital-currency-addresses
- Chainalysis free sanctions API: https://auth-developers.chainalysis.com/sanctions-screening/api-reference/reference/check-if-an-address-is-sanctioned
- Chainalysis free tools overview: https://www.chainalysis.com/free-cryptocurrency-sanctions-screening-tools/
