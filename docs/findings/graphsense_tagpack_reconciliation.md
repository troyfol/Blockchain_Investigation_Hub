# Findings — GraphSense TagPacks → BIH attribution/entity (new free connector)

**Date:** 2026-06-28
**Method:** docs-sourced (official GraphSense repos, fetched 2026-06-28).
**Why:** lock the **free attribution pillar** (see [[project-bih-sourcing-architecture]] in memory / `docs/findings/`).
After the Arkham re-scope, `attribution`/`entity_membership` has **no correct producer** — this fills it.
**Target models:** `models/claims.py::Attribution` (`address_id, label, category, source, confidence,
note, retrieved_at`), `Entity`, `EntityMembership`.

## TL;DR

GraphSense TagPacks are **free, open (MIT), public-source** attribution tags in YAML, designed for
*provenance-aware* sharing — they map almost one-to-one onto BIH's `attribution` + `source_query` spine
(Invariants #3, #4). This is a **new connector** (not a re-scope). It ingests YAML tag files from the
public TagPack Git repo; `Attribution.note` already exists to hold each tag's dereferenceable source
link, so no model change is needed for the core. Bonus: tags carrying an `abuse` value double as a
**free categorical risk** signal.

## The TagPack format (confirmed)

A TagPack is a YAML file: a **header** (`title`, `creator` mandatory; `description` optional) plus a
**body** = list of `tags`. Any body field can be **abstracted into the header** and inherited by all
tags (and `header: !include header.yaml` includes a shared header). Tag fields:

| Field | Mand. | Meaning |
|---|---|---|
| `address` | yes | the address |
| `label` | yes | human label (e.g. `Internet Archive`) |
| `source` | yes | dereferenceable backlink to where the tag came from |
| `currency` | yes | `BTC`/`ETH`/`BCH`/`LTC`/`ZEC`/`XRP`… |
| `category` | no | entity type, from the INTERPOL DW-VA **Entity** taxonomy (exchange, wallet_service, organization…) |
| `abuse` | no | abuse type, from the INTERPOL DW-VA **Abuse** taxonomy (scam, ransomware, sextortion…) |
| `confidence` | no | an **id** from a confidence taxonomy (not a float — see below) |
| `is_cluster_definer` | no | bool: does this single-address tag apply to the whole cluster |
| `context` | no | arbitrary JSON string |
| `lastmod` | no | datetime |
| `actor` | no | links to an **Actor** (real-world entity) defined in an ActorPack |

Tag identity = **(`address`, `label`, `source`)** — the natural upsert key (Invariant #7). The same
(address,label) from different parties is intentionally kept side-by-side (Invariant #4).

**Confidence is categorical → numeric.** `confidence` is an id from `confidence.csv`, each with a
`level` 0–100: `ownership`/`ledger_immanent`/`override` = 100, `manual_transaction` = 90,
`service_api`/`forensic_investigation` = 70, `authority_data` (OFAC etc.) = 60,
`service_data`/`forensic`/`trusted_provider` = 50, `web_crawl` = 20, `heuristic` = 10, `unknown` = 5.
→ map to BIH `Attribution.confidence` as **`level / 100`**, and keep the id text in `note`.

## Mapping GraphSense → BIH

| GraphSense | BIH target | Notes |
|---|---|---|
| `address` + `currency` | `Address(chain, address_display)` | **currency→chain map**; canonicalize per chain |
| `label` | `Attribution.label` | always present (unlike Arkham) |
| `category` | `Attribution.category` (+ `Entity.entity_type` if actor) | store the taxonomy concept raw |
| `confidence` (id) | `Attribution.confidence` = `level/100` | bundle `confidence.csv`; keep id in `note` |
| `source` (backlink) | `Attribution.note` | per-tag provenance link — `note` field already exists |
| `abuse` | **also** `RiskAssessment(category=abuse, source='graphsense', score=None, rationale=label/source)` | free categorical-risk bonus |
| `actor` | `Entity(origin='source', name, entity_type=category)` + `EntityMembership(method='tagpack-actor')` | needs ActorPack ingest (phase C) |
| `is_cluster_definer` | `EntityMembership.flags` | cluster-applicability signal |
| TagPack file Git URI | `source_query` (connector=`graphsense-import`, raw_response=YAML bytes) | Invariant #3 |

## Reconciliation points / divergences

1. **YAML, not CSV** — parser must resolve **header inheritance** and `!include`.
2. **confidence taxonomy** — categorical id → `level/100` float; bundle/vendor `confidence.csv`.
3. **currency→chain + unsupported chains.** BIH v1 = **BTC + EVM only** (`canonical.py`: bitcoin is
   UTXO, everything else treated as EVM and will **raise** on a non-EVM-looking address). So map
   `BTC→bitcoin`, `ETH→ethereum` (+ any EVM currency codes) and **skip + report** BCH/LTC/ZEC/XRP/… —
   same pattern as the Arkham tron skip. Don't let them hit `canonical_address`.
4. **Idempotency** — upsert on (address, label, source) (Invariant #7).
5. **abuse = dual-purpose** — feed `risk_assessment` too; partial free coverage of the risk pillar.
6. **actor = entities** — `Entity` + `EntityMembership` from ActorPacks; phase it after tags.
7. **never merge** — keep duplicate (address,label) tags from different TagPacks side-by-side (Inv #4).

## Ingest approach

Source = the public TagPack Git repo (`github.com/graphsense/graphsense-tagpacks`, `packs/`), a
**structured import of public data** → fits Invariant #1 (no scraping). Subclass the existing
`ImportConnector` (file → `raw_response` → parse), but parse **YAML** instead of CSV; capability
`get_attributions` (+ `get_risk` for `abuse`). Read a local clone/download of the repo (don't fetch
per-call). Bundle `confidence.csv`; store `category`/`abuse` concepts raw (optionally validate against
the INTERPOL DW-VA taxonomies). ActorPacks (entities) are a second, separate YAML type.

**Suggested phasing (CLAUDE.md §7 small steps):**
- **A (core, the free attribution pillar):** tags → `attribution`; confidence map; currency→chain filter
  + skip/report; idempotent upsert; per-file provenance.
- **B:** `abuse` → `risk_assessment` (free risk bonus).
- **C:** ActorPacks → `entity` + `entity_membership`; `is_cluster_definer` → flags.

## Sources

- TagPack wiki / schema: https://github.com/graphsense/graphsense-tagpacks/wiki/GraphSense-TagPacks
- Public TagPacks repo: https://github.com/graphsense/graphsense-tagpacks
- Confidence taxonomy: https://raw.githubusercontent.com/graphsense/graphsense-tagpack-tool/master/src/tagpack/db/confidence.csv
- Entity/Abuse taxonomies (INTERPOL DW-VA): https://graphsense.github.io/DW-VA-Taxonomy/
