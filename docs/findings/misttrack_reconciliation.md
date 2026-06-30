# Findings — MisTrack: assumed CSV parser vs. the real MisTrack API

**Date:** 2026-06-28
**Method:** docs-sourced (no account needed). Confirmed against MisTrack's official OpenAPI docs
(`docs.misttrack.io`, fetched 2026-06-28) — the same "verify the real shape before trusting the
parser" pass we ran for Arkham.
**Inputs:** assumed parser `backend/app/connectors/imports/misttrack.py` + fixture
`backend/tests/fixtures/imports/misttrack_sample.csv`; `models/claims.py::RiskAssessment`.

## TL;DR

Same structural finding as Arkham: **MisTrack is an API, not a CSV export.** The parser assumes a flat
CSV (`address, chain, risk_score, risk_level, category, label`) that MisTrack does not emit. The real
product is `openapi.misttrack.io` returning JSON, and the data the parser wants is split across **two
different endpoints** with richer, nested shapes. `misttrack.py` should be re-scoped to an **API
connector** (Invariant #1 — official API is the preferred path), not a manual-import parser — unless we
confirm the MisTrack dashboard offers a CSV export (can't verify without an account; if it does, the
import parser must match that real export, which stays a `TODO: confirm`).

## What the parser assumes

CSV `address, chain, risk_score, risk_level, category, label` →
- `get_risk` → `RiskAssessment(score=risk_score, score_scale="0-100", category=category, rationale=label)`
- `get_attributions` → `Attribution(label=label, category=category)`

## What MisTrack actually provides

### Endpoint 1 — Risk Score (`GET /v2/risk_score`, also a **V3.0** and async variants)

`?coin=ETH&address={addr}|txid={hash}&api_key=...` → `data`:

| Field | Type | Notes |
|---|---|---|
| `score` | int | **Range 3–100** (not 0–100) |
| `risk_level` | string | enum **`Low` / `Moderate` / `High` / `Severe`** (capitalized) |
| `detail_list` | list[str] | human-readable risk items, e.g. `"Involved Illicit Activity"` |
| `risk_detail` | list[obj] | the substance — see below |
| `hacking_event` | string | named security incident, if any |
| `risk_report_url` | string | link to a downloadable AML PDF report |

`risk_detail[]` objects: `entity` (e.g. `garantex.io`), `risk_type`
(`sanctioned_entity`/`illicit_activity`/`mixer`/`gambling`/`risk_exchange`/`bridge`), `exposure_type`
(`direct`/`indirect`), `hop_num` (int ≥1), `volume` (USD float), `percent` (float).

Risk-level bands: Low 0–30, Moderate 31–70, High 71–90, Severe 91–100. Risk is computed per
**blockchain** (all coins on the same chain return the same result).

### Endpoint 2 — Address Labels (`GET /v1/address_labels`)

`?coin=ETH&address={addr}&api_key=...` → `data`: `label_list` (e.g. `["Binance","hot"]` = entity +
wallet-type tags) and `label_type` (enum `exchange`/`defi`/`mixer`/`nft`/empty).

## Reconciliation (assumed → real)

| Parser assumes | Reality |
|---|---|
| flat CSV import | **API** (`openapi.misttrack.io`), JSON. Manual-import premise likely wrong (cf. Arkham). |
| `chain` column | **`coin`** query param (ETH/BTC/USDT-TRC20…); needs a chain→coin map. Risk is per-chain. |
| `risk_score` 0–100 | `score` **3–100** — `score_scale="0-100"` is wrong; use `"3-100"`. |
| `risk_level` low/high | enum `Low/Moderate/High/Severe`; **and the parser doesn't even store it** — `RiskAssessment` has no `risk_level` field. |
| `category` (risk) | no single category on the risk response; closest is per-item `risk_type`, or `label_type` from the **labels** endpoint. |
| `label`→`rationale` | closest is `detail_list` (risk items) or `hacking_event`; the assumed single `label` doesn't exist. |
| — | **`risk_detail[]` (nested) is the real value** (entity, risk_type, exposure direct/indirect, hop_num, volume, percent) and has **no slot** in `RiskAssessment`. Major modeling gap. |
| attribution from same row | attribution is a **separate endpoint** (`address_labels`): `label_list` (entity + wallet tags) + `label_type` → the parser's `category`. |

## Implications / proposed actions (for the next batch)

1. **Re-scope `misttrack.py` from CSV-import to an API connector** (`connectors/misttrack.py`, not
   `connectors/imports/`), capabilities `get_risk` + `get_attributions`, hitting `/v2` (or **V3** —
   confirm which to target) `risk_score` and `/v1/address_labels`. Keep raw-per-source storage and
   Invariant #4 (never combine scores). Mirror the Arkham re-scope.
   - Caveat: if you specifically want the manual-import path (no API budget), the import parser must
     match a real **dashboard CSV export** — unverifiable without an account → `TODO: confirm` (I can
     pull it via the Chrome flow if you get access).
2. **`coin` vs `chain`:** add a chain→coin map; document that risk is per-blockchain.
3. **Fix the score scale:** `score_scale="3-100"`; capture `risk_level` (extend `RiskAssessment` or map
   into `rationale`). Encode the Low/Moderate/High/Severe bands.
4. **Capture `risk_detail`:** decide storage for the nested breakdown (a `risk_detail` child table, or
   raw JSON in `rationale`) — today it would be silently dropped. Also `detail_list`, `hacking_event`,
   `risk_report_url` (the PDF is a useful provenance artifact).
5. **Attribution:** source from `address_labels` — `label_list` → `Attribution.label` (entity + tags),
   `label_type` → `Attribution.category`. Not from the risk endpoint.
6. **Confirm endpoint version (V2 vs V3.0)** and re-confirm V3 field names before building; mark unknowns
   `TODO: confirm` per CLAUDE.md §6. Update `docs/connectors.md §6` (MisTrack row) and `PROGRESS.md`.
7. **Provenance:** the API connector still writes a `source_query` per call (Invariant #3); the import
   base's file-as-raw_response no longer applies — store the JSON response as `raw_response`.

## Sources

- Risk Score: https://docs.misttrack.io/api-endpoints/get-risk-score.md
- Address Labels: https://docs.misttrack.io/api-endpoints/get-address-labels.md
- Address Overview: https://docs.misttrack.io/api-endpoints/get-address-overview.md
- Docs index: https://docs.misttrack.io/llms.txt
