# Blockchain Investigation Hub (BIH)

A **provenance-first integration-and-reporting hub** for blockchain investigations. It orchestrates data
from public tools and APIs, normalizes everything into one truthful data model where **every fact carries
the source query that produced it**, gives you an investigation graph, and emits **investigation-grade,
reproducible case files**.

It is deliberately **not** an analytics/clustering engine that hands you a single answer. Conflicting
claims from different sources are preserved side-by-side and **never averaged** into a synthetic score.
The tool integrates, attributes, and lets *you* reason — with a defensible, hash-verified record of where
every number came from.

> **Single-user and local by design.** No accounts, no server multi-tenancy, no telemetry. A case is a
> self-contained folder you can zip, hand to a colleague, and re-open with its hashes intact.

This README is the comprehensive orientation for **developers and LLMs**: §1–§3 are the product;
**§4 onward is the architecture deep-dive** (invariants, data model, connectors, services, audits, code
map). The machine-checked contract every change must uphold lives in **`CLAUDE.md`**; per-area docs in
**`docs/`**.

---

## Sample case — Tornado Cash

A real end-to-end run on the OFAC-sanctioned **Tornado Cash** router
`0x722122dF12D4e14e13Ac3b6895a86e84145b6967`: ingest → value → OFAC/GraphSense intel → clustering + the
Leiden community overlay → a curated, bounded report. The artifacts below ship with the release in
[`examples/Sample_Tornado_Cash/`](examples/Sample_Tornado_Cash/).

<img width="1882" height="1016" alt="tornado_screenshot" src="https://github.com/user-attachments/assets/95edb7f8-ea64-4d2f-afb4-273bf9a88a01" />

*The sanctioned anchor carries a red halo + "Tornado Cash" ring; thousands of same-pair movements collapse into one "… ×N" edge so the mixer stays legible. Stylized in-app view.*


- **Report:** [`report.html`](examples/Sample_Tornado_Cash/report.html) (self-contained, hash-verified) ·
  [`report.pdf`](examples/Sample_Tornado_Cash/report.pdf)
- **Portable bundle:** [`Sample_Tornado_Cash.casefile`](examples/Sample_Tornado_Cash/Sample_Tornado_Cash.casefile)
  — open it in BIH (*Import case*) to explore the exact case, hashes intact.
- **Reproduce it:** `python scripts/build_showcase.py` (live; needs a free Etherscan key in the OS keyring).

---

## 1. What it does

- **Two chains, one truthful model.** EVM movements are `transfer` facts (A→B happened). Bitcoin is
  `tx_input`/`tx_output` only — BIH **never** fabricates a Bitcoin input→output "transfer" (the ledger
  doesn't record which input funded which output). That linkage exists only inside a **trace**, labeled.
- **Free sourcing pillars + optional paid sources.** Facts from Etherscan (EVM) and Esplora (Bitcoin);
  the **free attribution pillar** is GraphSense TagPacks; the **free risk pillar** is OFAC SDN sanctions
  (+ Chainalysis). Optional paid sources (Bitquery, Arkham API, MisTrack, OKLink) are disabled by default,
  side-by-side, and never block the free baseline.
- **Value at the time it moved.** Optional USD valuation prices each movement at its block timestamp
  (DeFiLlama, batched), with exact decimal math. A missing price is shown as missing — never a zero.
- **Entity resolution you can audit.** Bitcoin co-spend clustering, CoinJoin flagging, merge/split — all
  reversible, provenance-tracked, with contested attributions shown side-by-side. Opt-in clustering
  heuristics faithful to the literature (BlockSci change-address, Victor 2020 EVM deposit-reuse, Leiden
  community-as-visual-structure) each produce **separate, confidence-tagged, reversible** cluster claims —
  never one merged answer (see §8a).
- **Traces, findings, annotations, tags.** EVM traces reference real `transfer` facts; Bitcoin FIFO
  tracing is a labeled *convention* (with manual override), never presented as ground-truth flow.
- **An investigation canvas that shows what it knows.** The graph encodes intelligence visually — the
  seed/anchor address (where the investigation started) carries a **centered ★ marker** (cyan `#4deeea`,
  `node.seed.marker`); sanctioned/risk nodes get an unmistakable halo + badge (distinct from the selection
  ring); sourced entities show their label and grouping; `possible-coinjoin` membership is flagged; and
  FIFO trace links render as a visibly dashed *convention* so a convention never looks like a fact.
  Clicking a node (or a flow/edge) opens a side panel with every source's claims side-by-side; the graph
  and side-panel label sizes are independently adjustable. The current focused/filtered view can be saved
  as a standalone court-ready **exhibit image** — SVG (vector, preferred) or PNG — rendered in the
  ink-light print palette so it reads on paper (a view artifact; it never changes the case). **All colors
  resolve through one named theme-token catalog** (`frontend/src/theme/`) — a single source of truth the
  Cytoscape stylesheet and the report CSS both consume, and the basis for the upcoming user
  color-customization UI.
- **Finality before immutability.** A fact is frozen only once its transaction has ≥ the per-chain
  confirmation threshold; until then it's `provisional` and correctable. A re-fetch upgrades it.
- **Immutable reports + portable case files.** A report is a self-contained HTML page that renders the
  real interactive graph; its SHA-256 `content_hash` is frozen over that HTML (the engine-independent
  source of truth). The PDF is printed by the **OS browser engine** already on the machine (Edge/WebView2
  on Windows, system Chrome/Chromium elsewhere — no bundled Chromium; Playwright optional) — a rendered
  artifact, not what the hash covers. Export writes a `<case>.casefile` with a hash manifest that
  re-validates on open and proves the case is self-contained.
- **Validated against real cases.** A growing suite of `make smoke` guards recreates **five real
  LEA/FIU-validated on-chain cases** (Ronin/Lazarus, Colonial Pipeline, Bitfinex 2016, Hydra/Garantex,
  a Whirlpool CoinJoin) and diffs BIH's output against the published ground truth (`docs/validation/`).

---

## 2. Quickstart

```bash
make setup                     # install deps (+ optional Playwright Chromium; reports otherwise print
#                                with your OS browser engine). add .[app] for the desktop window
make app                       # ONE-CLICK desktop app: builds the UI, opens a native window
make run                       # dev mode: FastAPI backend + Vite dev server (hot reload)
make migrate CASE=cases/acme/case.db   # create/upgrade a case database (forward-only migrations)
make report  CASE=cases/acme/case.db   # render an immutable PDF report of the case
make retest                            # re-run the 3 verification cases end-to-end (live) into app-data
make export  CASE=cases/acme/case.db   # zip + hash-manifest the case to acme.casefile (and verify)
make test && make audit && make smoke  # full suite, invariant audits, golden smoketests — the green gate
```

**Dev hot-reload.** `make run`'s backend runs under `uvicorn --reload` **scoped to `backend/app`**, so a
route or code edit is picked up without a manual restart. The watcher is deliberately not pointed at the repo
root — `cases/`, `raw_responses/`, and the built `frontend/dist` churn would thrash the reloader and silently
miss real edits. The **packaged app does not hot-reload, by design**: `make app` runs the server in-process on
a pre-bound socket so the splash / teardown / single-instance lifecycle owns that one process.

**One-click launch.** Double-click `launch.cmd` (Windows) / `launch.sh` (macOS/Linux) or run `make app`:
it serves the built SPA + API on one private `127.0.0.1` port and opens the investigation in a native
pywebview window. API keys live in the **OS keyring**, never in a file (`backend/app/secrets.py`; the one
loud `BIH_ALLOW_PLAINTEXT_KEYS=1` env opt-in is dev-only). Tech stack: **Python 3.12+ / FastAPI / SQLite
(stdlib `sqlite3`, WAL) / yoyo raw-SQL migrations / httpx / keyring / Playwright**; frontend **React 19 +
Cytoscape.js** (Vite).

**Cases.** The app opens to a **case picker** — create a new case, open an existing `case.db`, or import a
`.casefile`. Import **verifies the bundle first** (hashes + self-containment); a tamper-failed bundle is
reported loudly and is **not** opened unless you explicitly accept it as untrusted. Opening or importing a
case only **reads** it — nothing in a bundle is ever executed. The launcher reopens your last-active case
(else shows the picker), and the in-app **Cases** button switches at any time. In the windowed app these use
native OS file dialogs; in the browser (`make run`) they fall back to file upload + a path field. The known
cases (Recent) live in a small per-user registry; "remove from list" never deletes the case on disk.

**Ingest data & generate a report — all from the UI.** A new case is empty; click **+ Add address** to pull
on-chain facts into it. The chain is auto-detected from the address format — a Bitcoin address ingests
**keyless** (Esplora); an `0x…` EVM address needs a free **Etherscan** API key, entered once in
**Settings → Connectors** (write-only to the OS keyring, like every key — never shown again) with a chain
selector (Ethereum/Arbitrum/Optimism/Base/Polygon) and a depth control (a first pull is always bounded; the
app says when the result is partial). Errors are honest — offline mode, a missing key, or an upstream
failure each get a clear message, never a stack trace. A busy address shows **live progress** (pages
fetched, or "rate-limited — backing off") with a **Cancel** button that stops cleanly — a canceled ingest
leaves the case consistent (no half-written data). Facts appear on the canvas **immediately**; pricing runs
**separately** in the background (and via the **Value** button), so a slow, rate-limited valuation never
makes the ingest look hung. **Find** (the search box) is different: it only *centers* the view on an
address already in the case. **Value** prices the case's movements at their block
time via DeFiLlama (free, no key; offline-aware — a missing price stays unpriced, never a fabricated
zero), so USD mode lights up. When you're ready, **Report** generates the immutable report of **exactly
the view you're looking at** (your focus/hops, folded dust/spam/poison, denomination grouping, USD-vs-native)
— the scope panel lists every filter applied and how many movements it hid, so the exhibit is reproducible
and honest about what's shown vs omitted. It shows the `content_hash` (the immutability proof) and where
the HTML/PDF landed, and opens the PDF via the OS browser engine; with no Edge/Chrome present the HTML
report is still produced (the PDF is a clean skip, not an error). The report leads with an explicit
**Risk & sanctions** section — every screened address that carries a risk/sanctions claim, each source
listed **side-by-side** (OFAC SDN, GraphSense abuse, Chainalysis — never merged or averaged, Inv #4),
sanctioned addresses first — kept distinct from the **Entities** (GraphSense attribution) section below it.

**Re-run the verification cases (`make retest`).** `scripts/retest_cases.py` re-runs the three reference
addresses end-to-end into the app-data cases folder (`%APPDATA%\BlockchainInvestigationHub\cases\<case>\`):
for each it creates a fresh case → ingests facts (Etherscan for EVM, which needs the keyring key and fails
loudly if absent; Esplora for BTC) → runs valuation **synchronously to completion** (so the report is fully
valued, not a half-priced async snapshot) → runs Check intel → generates the HTML+PDF report, then prints a
per-case summary (sanctioned addresses + source, attribution count, valuation M of N, report paths). It's a
live run (offline mode must be off). Expected: `test_tornado` → the anchor sanctioned, `test_vitalik` → its
Tornado counterparty sanctioned, `test_colonial` → clean.

**Reading values — USD or native units.** A **USD / Native** toggle switches what the graph ranks, sizes,
labels, and filters by: USD value-at-time, or raw native units (ETH/BTC). A movement with **no price** is
never treated as zero/dust — it falls back to its native amount, so a 100 ETH transfer DeFiLlama couldn't
price still renders large and stays visible. Native amounts are **per-asset** (never compared across
assets on one scale): native mode ranks/scales/thresholds within a single asset and groups a mixed-asset
view per asset. Right-click a node to order its neighbors by USD, by native amount, or by sequence;
expanding a dense node caps the visible neighbors and rolls the rest into a "show more" bundle; **Group
denominations** clusters counterparties sharing one exact native amount (mixer pools like 100/10/1 ETH).
The basis + ordering are remembered per case.

**Cutting through the noise (EVM).** Airdrop / brand-name spam tokens (unpriced but huge native amounts)
are collapsed into an "unverified / unpriced tokens" bundle by default — a **display de-emphasis, not a
claim that a token is malicious** (a *Show spam* toggle reveals them; native ETH and priced tokens stay
prominent). Likely **address-poisoning** (a zero-value transfer from an address whose first/last hex
mimics a real counterparty) is flagged with a reversible heuristic and folded out of the way. Each
denomination gets its own **min / fold** thresholds in its native units, so trimming one pool never
touches another. None of this is asserted as fact — it's reversible display logic over the real movements.

**Check intel (sanctions + attribution).** A live-ingested address shows on-chain facts only until you
click **Check intel**: it runs the free **OFAC SDN** sanctions + **GraphSense** attribution pillars
against the case from bundled snapshots (works **offline**; Chainalysis runs too if you set a free key),
writing provenance-carrying **sourced claims** — so the red sanctioned halo and the entity ring appear on
matching addresses. The claims sit side-by-side, never merged, and are never asserted as facts about the
chain. The bundled snapshots ship **real** OFAC SDN crypto-address designations (Tornado Cash, Garantex,
Lazarus Group, Hydra Market, SUEX, Chatex, …) with their entity attributions — a curated subset stamped
with its edition date (`2025-01-15`; the live SDN delisted Tornado Cash on 2025-03-21, so a dated
historical edition is the honest representation — use "Refresh from source" for the current list). The
EVM match is **case-insensitive** (OFAC publishes checksummed addresses; a case stores them lowercase —
both are canonicalized before comparison, Inv #8). Settings shows each snapshot's date.

**Legible exhibits on dense cases.** When many movements run between the same pair of nodes (e.g. a mixer
like Tornado: thousands of transfers among a handful of addresses), the canvas and the report **aggregate
parallel edges** sharing the same `(source, target, asset)` into one edge labeled with a count + summed
value (`… ×N`). This is a display rollup over real same-endpoint facts — never a synthesized transfer
(Inv #5); the individual movements stay reachable on drill-down (the flow inspector shows the collapse).
Annotated or flagged movements stay individual. Because pricing runs in the background, a report generated
before valuation finishes says so (`Valuation in progress at generation: M of N priced`).

**Build the desktop app (a standalone exe — no Python install needed).**

```bash
make package         # npm run build, then PyInstaller bih.spec -> dist/BIH/ (windowed, ~55 MB)
make package-debug   # same, but a console build that prints tracebacks (debugging a frozen failure)
make smoke-frozen    # run the built exe headlessly and assert the frozen DoD gate (15 checks)
```

`make package` produces a **one-folder** app at `dist/BIH/` — `BIH.exe` plus an `_internal/` folder
(everything it needs: the built SPA, migrations, report templates, the token catalog, the certifi CA, the
WebView2 runtime). Double-click `BIH.exe` to launch; no Python or Node is required on the target machine. The
report still prints via the **OS-installed Edge/Chrome** (no Chromium is bundled — that's why it's ~46 MB, not
~350 MB). The build is committed as `bih.spec`; `build/` and `dist/` are gitignored.

- **Where data lives (installed):** under the per-OS app-data dir — Windows `%APPDATA%\BlockchainInvestigationHub`,
  macOS `~/Library/Application Support/BlockchainInvestigationHub`, Linux `$XDG_DATA_HOME/...`. Settings, the
  case registry, logs, and any **new** cases you create go here. The bundle itself (`_internal/`) is read-only
  and is **never written to**.
- **Portable install:** drop a `portable.txt` next to `BIH.exe` (or set `BIH_PORTABLE=1`) and all user data is
  written to a `data/` folder beside the exe instead — a thumbdrive-friendly install that leaves no trace in
  `%APPDATA%`.
- **Scope:** Windows one-folder is the current deliverable. PyInstaller builds are **per-platform**, so a
  macOS/Linux app is a follow-on build on that OS — `bih.spec` already branches the per-OS keyring/pywebview
  hidden-imports.

**Build the Windows installer.**

```bash
make installer       # Inno Setup -> dist/installer/BIH-Setup-<ver>.exe  (a single UNSIGNED setup.exe)
make sign            # OPTIONAL: Authenticode-sign the exe + installer (no-op + clean skip with no cert)
make verify-sign     # verify signatures on whatever is signed (no-op when nothing is signed)
```

`make installer` runs the [Inno Setup](https://jrsoftware.org/isinfo.php) compiler over `installer/bih.iss`
(it's located on PATH / Program Files / the per-user winget path; if missing, the command prints the one-line
`winget install JRSoftware.InnoSetup` to add it). The produced `BIH-Setup-<ver>.exe`:

- installs **per-user** (no admin → `%LOCALAPPDATA%\Programs`) **or to Program Files** (the wizard offers
  "all users", which elevates) — your choice at install time;
- creates **Start-Menu + Desktop shortcuts** using `8.ico`;
- registers a **clean uninstaller** in *Apps & features*;
- on uninstall **leaves `%APPDATA%\BlockchainInvestigationHub` untouched** — your cases, case registry,
  settings, and logs survive an uninstall *and* a reinstall. (The installer has no delete-user-data step; the
  app writes user data only under `%APPDATA%`, which it never created.)

### Distribution (honest about *unsigned*)

The installer ships **unsigned** by default — no code-signing certificate is required to build it. Be upfront
with anyone you hand it to:

- **SmartScreen "Unknown publisher".** On download/first run, Windows SmartScreen will show a blue
  *"Windows protected your PC … unknown publisher"* prompt. This is expected for any unsigned binary: it is
  **friction, not a block**. Click **More info → Run anyway**. A code-signing cert (below) earns the publisher
  name and, over time, reputation that suppresses the prompt — but it is not required to run the app.
- **What we already do to look trustworthy without a cert.** The binary is **not metadata-blank** — `BIH.exe`
  carries proper Win32 version-info (Company, Product, Version, Copyright, Description), a common
  SmartScreen/AV red-flag when absent. We deliberately **do not UPX-pack** the exe (the packer/unpacker stub
  pattern-matches as malware to several AV engines, a classic false-positive trigger). These reduce — but
  cannot eliminate — unsigned-binary friction.
- **Enable signing later — a one-line config change.** When you obtain a cert, set **one** of:
  `BIH_SIGN_PFX` + `BIH_SIGN_PASSWORD` (a `.pfx`) **or** `BIH_SIGN_THUMBPRINT` (an installed cert-store cert).
  Then `make sign` (and `make installer`) sign automatically with `signtool` + an **RFC3161 timestamp** (so
  signatures outlive the cert's expiry); `make verify-sign` checks them. Nothing else changes — no code, no
  spec, no `.iss` edits. We **do not** generate or ship a self-signed cert (a self-signed signature is no more
  trusted than unsigned and only adds confusion).
- **If an AV flags it (false positive).** Unsigned PyInstaller apps are occasionally mis-flagged. Confirm it's
  a false positive by uploading the exe/installer to **[VirusTotal](https://www.virustotal.com/)** (a handful
  of heuristic engines flagging while the majority are clean is the signature of a false positive), then submit
  the file to the flagging vendor's **false-positive / whitelist** form (most AV vendors have one) to get it
  cleared. Signing + accumulated SmartScreen reputation is the durable fix.

---

## 3. The three object families (the spine of the data model)

Three families, kept rigorously distinct, all in one `case.db`:

- **Raw facts (Family A)** — what a *chain* says: `asset`, `address`, `transaction_`, `transfer` (EVM),
  `tx_input`/`tx_output` (Bitcoin). Idempotent natural-key upserts; **immutable once their transaction is
  `final`**.
- **Sourced claims (Family B)** — what a *source* says: `attribution`, `risk_assessment`, `valuation`,
  `balance_snapshot`, `entity`, `entity_membership`. **Append-only**; multiple sources kept side-by-side,
  never collapsed.
- **Investigator objects (Family C)** — what *you* construct: `trace`/`trace_transfer`/`trace_btc_link`,
  `finding`/`finding_ref`, `annotation`, `tag`, `report`. Your reasoning, separated from sourced claims.

Every Family A/B row references the `source_query` that produced it, **written in the same DB
transaction** (`provenance/atomic.py`) — so provenance can never go missing.

---

## 4. The 8 invariants (the design's load-bearing rules)

These are in `CLAUDE.md` and enforced by runnable audits (§8). Understanding them explains most design
choices:

1. **No scraping.** Data enters only via official APIs or structured manual import of data a human
   legitimately accessed. Never automate against a third-party UI/ToS.
2. **Single-user, local.** No multi-user auth, no server multi-tenancy.
3. **Provenance on every fact.** Every raw fact / sourced claim references the `source_query` that
   produced it; the fact row and its `source_query` row are written in **one transaction**.
4. **Never collapse multi-source claims.** Different sources may disagree; store all, side-by-side. No
   averaged/synthesized risk scores, labels, or valuations — ever.
5. **The schema tells the truth on both chains.** EVM stores `transfer` (A→B is a fact). Bitcoin stores
   `tx_input`/`tx_output` only; **never** synthesize an input→output transfer as a fact. Input→output
   linkage exists only inside a `trace` as a labeled claim (`basis=fifo|investigator`).
6. **Finality before immutability.** A fact is immutable only once its tx is `final` (confirmations ≥
   per-chain threshold). `provisional` (tip) facts may be corrected/deleted on re-fetch.
7. **Idempotent ingest.** Re-fetching the same data upserts on natural keys; never duplicates.
8. **Canonicalize addresses on ingest.** Store the canonical form in `address.address`; keep the source
   display form in `address.address_display`.

---

## 5. Data model (schema)

SQLite, created by forward-only `yoyo` migrations under `backend/app/migrations/` (`schema_version` in
`case_meta`; currently **v6**, migrations `0001`–`0010`). Canonical Pydantic shapes in
`backend/app/models/`. Repository (idempotent upserts + append-only inserts + final-row freeze):
`backend/app/db/repository.py`.

**Provenance + container** (`0001`): `case_meta` (single row), `source_query` (the provenance spine — every
fact/claim FKs it; stores `connector`, `capability`, `endpoint`, `params` incl. recorded bounds,
`raw_response_ref` + `raw_response_hash`), `exhibit` (screenshot-as-hashed-evidence fallback).

**Family A facts** (`0002`): `asset` (unique `(chain, COALESCE(contract,''))`), `address` (unique
`(chain, address)`, canonical + display), `transaction_` (`finality_status` provisional|final,
`confirmations`), `transfer` (EVM; **dedup key is content + `occurrence`**, not source-dependent
`position` — migration `0007`, see §6), `tx_output`/`tx_input` (Bitcoin; `spent`/`spending_tx_id` refresh
allowed post-final).

**Family B claims** (`0003`): `attribution` (label/category/confidence/note), `risk_assessment` (score +
`score_scale` OR categorical `score=None`), `valuation` (poly subject transfer|tx_output), `balance_snapshot`,
`entity` (`origin` cospend-cluster|source|investigator, `merged_into` tombstone, `external_id` for source
entities — migration `0006`), `entity_membership` (+ append-only `entity_membership_retraction`).

**Family C investigator** (`0004`): `trace`, `trace_transfer` (EVM edge → a real `transfer`),
`trace_btc_link` (Bitcoin edge with `basis`), `finding`/`finding_ref`, `annotation`, `tag`, `report`.

**Read-model views** (`0005`): `v_value_movement` — the paradigm-agnostic projection: EVM transfers give
`(src, dst)`; Bitcoin outputs give `(NULL src, dst)` (**src is deliberately NULL** — which input funded an
output is not a ledger fact, Invariant #5). `v_address_flow` joins valuation.

---

## 6. Cross-source transfer reconciliation (decision (c), migration 0007)

A `transfer` is a **fact**, so the same on-chain movement reported by two sources (Etherscan = receipt-log
order, Arkham/Bitquery = row order) must dedup to **one** row, never double-count. `position` is
source-dependent, so it can't be the identity. The dedup key is the movement's **content**
`(transaction_id, transfer_type, from, to, asset, amount)` **+ `occurrence`** (a 0-based ordinal among
identical-content movements, stamped by `normalization/reconcile.py::assign_occurrences` before the write).
`position` is kept as a display ordinal. The same movement from two sources dedups (first-writer provenance
wins; the second source's `source_query` is still persisted); genuinely **disagreeing** facts (different
amount/parties) have different content → distinct rows kept side-by-side (Inv #4); legitimately-repeated
identical movements stay distinct via `occurrence` (Inv #7).

---

## 7. Connector catalog

Connectors **acquire data and own provenance**: each call writes its own `source_query` with the raw
response hashed (`backend/app/connectors/`). HTTP connectors share `base.py` (rate limit + 429/5xx
backoff); import connectors share `imports/base.py` (file → hashed `raw_response` → parse). Pure mapping
lives in `normalization/*_adapter.py` so nothing downstream knows a source's native shape.

**Free pillars (enabled by default):**

| Connector | Capabilities | What it does |
|---|---|---|
| **Etherscan V2** (`etherscan.py`) | `get_transactions`, `get_balance` | EVM facts; one key, many chains via `chainid`. Merges txlist + txlistinternal + tokentx (each its own `source_query`) → `transaction_`/`transfer`(native/internal/erc20). Honors `bounds` (block_range/time_window/min_value/direction/top_n/max_pages). |
| **Esplora** (`esplora.py`) | `get_transactions`, `get_balance`, `get_transfers` | Bitcoin/UTXO; cursor pagination; tx-as-node + `tx_input`/`tx_output` ONLY (Inv #5); spent-output marking; public `tip_height` (drives finality refresh). |
| **DeFiLlama** (`defillama.py`) | `get_price`, `get_prices` (batched) | USD price at a block timestamp; `coingecko:<slug>` (native) / `{chain}:{contract}` (token) keys; batched comma-joined per timestamp. Missing price ⇒ no row. |
| **GraphSense TagPacks** (`imports/graphsense.py`) | `get_attributions`, `get_risk`, `get_entities` | **Free attribution pillar.** Free/open YAML TagPacks (header→tag inheritance + `!include`) → `attribution`; `abuse` → categorical `risk_assessment`; ActorPacks + tag `actor` → `entity`/`entity_membership`. Hardened YAML loader. |
| **OFAC SDN** (`imports/ofac.py`) | `get_risk`, `get_attributions` | **Free risk pillar.** Parses the official SDN XML; `"Digital Currency Address - <TICKER>"` → ticker→chain map → categorical `risk_assessment(category='sanctioned')` + optional `sanctioned_entity` attribution. Mutable list: delisting reported, not deleted. |
| **Chainalysis** (`chainalysis.py`) | `get_risk` (free key) | Second sanctions source, side-by-side with OFAC (Inv #4); records the check even when clean. |

**Optional paid sources** (`connectors/registry.py` gates them: available only when `BIH_<name>_ENABLED`
**and** a keyring key are both set; otherwise silently absent). All disabled by default.

| Connector | Capabilities | Notes |
|---|---|---|
| **Bitquery** (`bitquery.py`) | `get_transactions`, `get_transfers` | GraphQL multi-chain EVM facts fallback (V2 Bearer). Query body `TODO: confirm`. Keyring `bitquery_token`. |
| **Arkham API** (`arkham.py`) | `get_attributions`, `get_risk` | Path B attribution: confirmed `arkhamEntity`→entity, `predictedEntity`→a SEPARATE lower-confidence entity (never collapsed, Inv #4); numeric risk breakdown kept raw. Keyring `arkham_api_key`. |
| **MisTrack** (`misttrack.py`) | `get_risk`, `get_attributions` | API (the CSV importer was retired). Score 3-100; `coin` not chain. Keyring `misttrack_api_key`. |
| **OKLink** (`oklink.py`) | `get_attributions`, `get_risk` | **Shell** — confirmed conventions wired; AML endpoints `TODO: confirm` so capabilities raise "not wired". Keyring `oklink_api_key`. |

Also: `imports/arkham.py` is the Arkham **UI transfer-log CSV** importer (`get_transactions` → EVM
transfers; Bitcoin rows refused, Inv #5) — distinct from the Arkham **API** connector above.

---

## 8. Services (the investigation logic), provenance, and audits

**Services** (`backend/app/services/`):

- `orchestrator.py` — thin router: dispatches a fact capability to a connector by `capability` + `chain`
  (free connectors first, paid as fallback). Connectors own provenance.
- `valuation.py` — values unvalued movements (EVM transfers + BTC outputs) at their block timestamp,
  **batched by timestamp** via DeFiLlama; honest gaps (no fabricated zero) + circuit-breaker.
- `entities.py` — co-spend union-find clustering at ingest, CoinJoin flagging, merge (`merged_into`
  tombstone) / split (append-only retraction), same-address heuristic; `find_or_create_source_entity`.
- `entity_display.py` / `claims_display.py` — canonical/contested entities + side-by-side claims, NO
  combined score.
- `tracing.py` — EVM `add_trace_transfer` (references a real `transfer` fact); Bitcoin `fifo_apportion`
  (pure, conservation-guaranteed) → `trace_btc_link(basis='fifo')`, plus `add_manual_link`
  (`basis='investigator'`). No automated path discovery — a trace cannot fabricate flow.
- `finality.py` — `upgrade_finality`/`refresh_finality` (Inv #6): re-fetch tip, recompute confirmations,
  flip `provisional`→`final` at the threshold; final rows frozen; idempotent.
- `investigator.py` — findings/annotations/tags (poly-refs validated on write).
- `reporting.py` — immutable report: self-contained HTML (the real Cytoscape view), `content_hash` over
  that HTML, supersession; rendered to PDF by the OS browser engine (`report_render.py`).
- `export.py` — `build_manifest` (SHA-256 of `case.db` + `raw_responses/`/`exhibits/`/`reports/`/
  `.audit_baselines/`), `export_case` (defensive `wal_checkpoint` → manifest → zip `.casefile`),
  `verify_casefile` (hashes match + self-contained + provenance FKs resolve in-bundle).
- `graph.py` — projects `v_value_movement` (+ `tx_input`) into the heterogeneous graph for the API.

**Provenance writer** (`provenance/atomic.py`): `write_with_provenance` is the ONE way facts/claims enter
the DB — opens a SAVEPOINT, inserts the `source_query` (with the raw response's SHA-256), runs the caller's
write, commits atomically; the raw file is promoted by atomic rename only after the DB commit.

**Invariant audits** (`backend/app/audits/`, `make audit`) — runnable checks that FAIL LOUDLY, encoding
the invariants as queries: **provenance-completeness, no-dangling-fk, idempotency, final-immutability,
no-fabricated-utxo-edge (Inv #5), append-only-claims, entity-resolution-sanity, cache-provenance-carried,
valuation-subject-validity, bounds-recorded** (10/10 must pass). The two cross-run checks persist a
baseline sidecar (`.audit_baselines/`) that ships with the case.

---

## 8a. Clustering algorithms (faithful to the published definitions)

Every clustering heuristic is a **separate, named, versioned producer** that writes its own `source_query`
(Invariant #3) and a **per-membership confidence**, reusing the `entity` / `merged_into` / `entity_membership`
/ append-only `entity_membership_retraction` spine (`services/clustering/`). So every heuristic is **reversible
by construction** — split one address out (a retraction) or **undo a whole run as a unit** (retract every
membership written by that run's `source_query`). They are **never merged into one answer**: co-spend and each
new heuristic produce **side-by-side** cluster claims (Invariant #4); an address can belong to several at once,
each shown with its forming heuristic + confidence. **Conservative defaults:** co-spend stays ON
(high-confidence, at ingest); **every new heuristic defaults OFF** and is applied explicitly from the Clustering
panel (preview → apply → undo). **CoinJoin gating** applies throughout: a probable-CoinJoin tx
(`is_probable_coinjoin`) is never used to link addresses, so a mixer never bridges two wallets.

- **Co-spend — Meiklejohn et al., "A Fistful of Bitcoins" (IMC 2013).** Common-input-ownership: addresses
  co-spent as inputs of one Bitcoin tx are one entity (union-find at ingest). CoinJoin-flagged inputs are
  excluded/`possible-coinjoin`-tagged (confidence 0.5 vs 0.9). *This is the always-on baseline.*

- **Bitcoin change-address — BlockSci 0.7** (`citp.github.io/BlockSci/reference/heuristics/change.html`).
  Each heuristic returns the *candidate change outputs* of a tx; the identified change is linked to the tx's
  inputs (same wallet). Implemented faithfully: **address_reuse** (an output whose address is among the
  inputs), **address_type** (the output sharing the inputs' single script type), **optimal_change** (an output
  smaller than the smallest input — *with BlockSci's documented single-input caveat: with one input every
  output qualifies*), **power_of_ten_value(digits)** (round 10^d outputs are the spend, so the change is among
  the non-round outputs), **client_change_address_behavior** (the output that is the first funding of its
  address — approximated within-case), and the dynamic **peeling_chain** (the spent continuation output of a
  2-output peel). `locktime` is **unavailable** (BIH does not store a tx's nLockTime — reported, never
  guessed). Composition mirrors BlockSci (`&` / `|` / `-` / `unique_change`), and — per BlockSci's **explicit
  guidance "We recommend against simply using one of these heuristics without further refinement for
  clustering"** — clustering **requires the agreement of ≥N heuristics** (each first reduced to its single
  `unique_change` candidate), never a single bare heuristic. **The default is ≥2** (a single heuristic
  merging is exactly what BlockSci warns against); `≥1` is available only as an explicit, clearly-marked
  "permissive — false-positive-prone" opt-in in the Clustering panel. Confidence rises with the agreement
  count. (Co-spend — a distinct high-confidence heuristic at 0.9 — is unaffected by this threshold.)

- **Ethereum (EVM) — Friedhelm Victor, "Address Clustering Heuristics for Ethereum" (FC 2020)**
  (`ifca.ai/fc20/preproceedings/31.pdf`, §5).
  - **Deposit-address reuse (primary).** A deposit address `v_d` sits on `v_u → v_d → v_e` with `v_e` a known
    exchange: same asset type, `0 ≤ received − forwarded ≤ a_max` (paper default **a_max = 0.01 ETH**; tokens
    0), `0 ≤ Δblock ≤ t_max` (**3200**), `v_u ∉ exch∪miners`, `v_d ∉ exch`, and `v_d` forwards to **exactly one**
    exchange. Users sending to the **same** `v_d` are clustered. The documented **masquerade false-positive**
    (an adversary forwards a received amount to an exchange so their address looks like a deposit and pulls the
    sender into their cluster) is encoded as a **confidence reducer + `masquerade-risk` flag** on thin deposits.
    "Known exchange" comes from the attribution pillar (`category` ~ exchange).
  - **Airdrop multi-participation.** Recipients of one fixed-amount airdrop (≥1000 recipients) who forward the
    **exact** received amount into a common aggregator (`agg_min = 2`; aggregator not an exchange/DEX) are one
    entity; entities capped at ≤1000 addresses.
  - **Self-authorization.** An ERC-20 `Approval(owner, spender)` with both active EOAs (exchange spenders
    removed; bounded ≤10 each way) links owner↔spender. **Data-gated:** the default Etherscan pull fetches
    Transfer events only, so this reads the `erc20_approval` table — populated on demand via
    `POST /api/approvals/fetch` (Etherscan `getLogs`, `topic0` = the `Approval` event) or a structured import —
    and is a clean no-op when empty (an honest "no approval data" result, never a fabricated link).

- **Community detection — Leiden; Traag, Waltman & van Eck, "From Louvain to Leiden" (Sci. Rep. 2019)**
  (`arxiv 1810.08473`). **VISUAL STRUCTURE ONLY — never an ownership claim, never persisted.** A community is
  the output of optimising a resolution-parameter quality function over the *current view's* graph — a tunable
  lens, not an on-chain fact — so promoting it to ownership would manufacture an unprovenanced, synthesized
  claim (Invariants #3/#4 forbid it). It is computed at view time via **python-igraph's native Leiden**
  (`Graph.community_leiden`, an optional GPL dependency, import-guarded) and rendered as a **distinct dashed
  violet `group_type='community'` box labelled "structure, not ownership"**; it writes no `entity_membership`.
  **Leiden, not Louvain**, because (the paper's central result) *"the Louvain algorithm may yield arbitrarily
  badly connected communities … communities may even be disconnected"* (up to 25% / 16% empirically), whereas
  Leiden's refinement phase **guarantees every community is internally connected** — so a community box never
  groups addresses that aren't even mutually reachable within it.

---

## 9. Data flow (end to end)

```
acquire (connector.get_*)              normalize (adapter)        write (repository, atomic+provenance)
  Etherscan/Esplora/Bitquery  ──▶  ParsedTransaction/Transfer ──▶ transaction_/transfer/tx_*  (Family A)
  GraphSense/OFAC/Chainalysis ──▶  ParsedTag/Sanction/...      ──▶ attribution/risk_assessment (Family B)
  DeFiLlama (valuation svc)   ──▶  compute_value (Decimal)     ──▶ valuation                   (Family B)
                                                          │
   read-model: v_value_movement / v_address_flow  ◀───────┘
                                                          │
   investigate: entities, traces, findings, annotations, tags   (Family C)
                                                          │
   present: /api/graph (Cytoscape)  ──▶  report (HTML+content_hash, PDF via OS engine)  ──▶  export (.casefile + manifest)
```

Every arrow that writes a fact/claim also writes its `source_query` in the same transaction. `make audit`
runs after any data write; nothing is "done" until `make test && make audit && make smoke` are green.

---

## 10. Default settings & how to tune them

Conservative defaults: the tool would rather call data `provisional` or a price *missing* than overstate
certainty. Knobs live in `backend/app/config.py` (overridable by a `BIH_`-prefixed env var) or at the top
of the named module. **Write the change down — it changes what "final" and "valued" mean in your case.**

1. **Per-chain finality thresholds — the most important setting** (`DEFAULT_FINALITY_THRESHOLDS`). A tx is
   frozen as immutable only at ≥ this many confirmations (Inv #6). Confirmed values: bitcoin **6**,
   ethereum **64** (≈2 epochs / consensus `finalized`), bsc **15** (BEP-126 fast finality), arbitrum/
   optimism/base **20** (L1-settlement proxy), polygon **128** (Heimdall v2 makes this over-conservative —
   a documented policy knob). Override via `BIH_FINALITY_THRESHOLDS` JSON (merged onto defaults so the
   settled bitcoin/ethereum can't be dropped).
2. **Expansion bounds** — `block_range`, `time_window`, `min_value`, `top_n_counterparties`, `max_pages`,
   `direction`. Absent = connector default (recorded in `source_query`); a truncating bound marks the
   query `partial`. Bounds limit *acquisition*, never delete already-ingested facts.
3. **Valuation precision** — `Decimal`, `ROUND_HALF_EVEN`, 18 fractional places, price at the block ts
   (`normalization/valuation_math.py`). Missing price ⇒ no row.
4. **CoinJoin / heuristic-confidence** (`services/entities.py`): `K_INPUTS=5`, `K_EQUAL_OUTPUTS=5`,
   Whirlpool denominations; co-spend confidence 0.9, CoinJoin-flagged 0.5, same-address 0.3 (never across
   the EVM/Bitcoin boundary).
5. **Paid connectors** — off by default; enable with `BIH_<NAME>_ENABLED=1` **and** a keyring key (see §7).
6. **In-app Settings UI** (header → **Settings**) — the runtime way to tune the above without env vars:
   toggle each paid connector and paste its API key (written **straight to the OS keyring**, write-only —
   the UI only ever shows *key set ✓ / no key*, never the value), change the **cases folder** (where new
   cases are created), and flip **offline mode**. **Offline mode** makes connectors refuse all outbound
   calls — ingest/expand are disabled and the app works only on already-ingested data; the graph, claims,
   reports, and export keep working. The panel shows a loud banner if no OS keyring backend is available,
   or if `BIH_ALLOW_PLAINTEXT_KEYS=1` is active (secrets read from env, not the keyring).
7. **Canvas theme** (header switcher: **Dark · Light · Custom**) — switch the on-screen palette instantly
   (the choice persists). **Custom** is the editable preset: the 🎨 **Customize** drawer gives every graph
   color a picker with a live canvas preview, per-token reset, and "Reset Custom to defaults" (back to the
   Neo-Tokyo palette); **Dark** and **Light** are locked modern themes. **Reports and exhibit image exports
   always render the print-light palette** regardless of the canvas theme, so case files stay paper-legible.
   Every color is a catalog token (`frontend/src/theme/tokens.json`) — no hardcoded hex anywhere.
8. **Data locations** (where your user data lives — registry of recent cases, `settings.json`, the
   single-instance lock, logs, and the default folder for NEW cases). **Installed:** the per-OS app-data dir
   — Windows `%APPDATA%\BlockchainInvestigationHub`, macOS `~/Library/Application Support/BlockchainInvestigationHub`,
   Linux `$XDG_DATA_HOME` (or `~/.local/share`)`/BlockchainInvestigationHub`. **Portable:** drop a
   `portable.txt` next to the executable (or set `BIH_PORTABLE=1`) and all user data is written to a `data/`
   folder beside the exe instead — thumbdrive-friendly, nothing left on the host. `BIH_APP_DATA_DIR` overrides
   the location entirely (used by tests). Bundled program files (the UI, migrations, report templates) are
   **read-only** and never written to; your data is always separate. (Running from source keeps new cases in
   the repo `cases/` folder; `BIH_CASES_ROOT` overrides it.)

---

## 11. Trust model (read this honestly)

- **Timestamps are local-clock**, not cryptographically notarized.
- **A report's `content_hash` is over its HTML, not its PDF.** The report is a self-contained HTML page
  (CSS + Cytoscape + the graph data inlined) — the reproducible source of truth. The PDF is a *rendered
  artifact*, printed by whatever OS browser engine is on the machine (Edge/WebView2, Chrome/Chromium;
  Playwright optional), and its bytes are **not** deterministic across engines/versions. So the frozen
  `content_hash` is the SHA-256 of the canonical HTML, and `rendered_file_ref` points at that HTML —
  which is what supersession, the export manifest, and cross-machine re-verification key off. The same
  HTML re-hashes identically anywhere; two engines may produce byte-different PDFs of it. A machine with
  no engine still produces a complete, hash-verifiable report (HTML only — the PDF is simply skipped).
- **Tamper-evidence, not tamper-proofing.** SHA-256 hashes (`manifest.json`, raw responses, report
  `content_hash`, `.audit_baselines/`) catch any change to an *intact* bundle. They do NOT stop an
  adversary who rewrites a file *and* its manifest entry together — the strong check is to re-export on a
  trusted machine and compare. Cryptographic non-repudiation / external notarization is a named deferred
  item.
- **API keys are write-only to the OS keyring.** Keys are stored only in the OS keyring (Windows
  Credential Manager / macOS Keychain / Linux Secret Service) and are **never read back to the UI,
  returned by any endpoint, or logged** — the Settings UI shows only presence/absence and its key input is
  a password field cleared on submit. The one exception is the loud `BIH_ALLOW_PLAINTEXT_KEYS=1` dev
  opt-in (keys read from env vars, never written to disk), which surfaces a prominent warning banner.
- **No synthesized conclusions.** No averaged risk score, no merged "the" label, no automated Bitcoin
  flow. Where sources disagree, you see the disagreement.

---

## 12. Testing, audits & validation

Layered tests (`docs/testing.md`): **unit** (pure logic), **contract** (replay recorded `cassettes/` →
canonical rows, offline), **integration/golden** (end-to-end ingest → DB + provenance), **property**
(Hypothesis: value conservation, idempotence, FIFO), **live-drift** (opt-in `RUN_LIVE=1`, confirms API
shapes). `make smoke` runs the golden end-to-end guards. **Cassettes double as provenance fixtures** —
real responses recorded once, committed, replayed.

**Real-case validation** (`docs/validation/`): BIH recreates **real LEA/FIU-validated on-chain cases**
end-to-end and diffs its output against the published ground truth + official designations — find-the-gaps,
not pass-the-test (divergences from the bounded real data are documented in each dossier's *Results*
section, never tuned away). Each is a permanent `make smoke` guard. Five cases cover the headline
capabilities and honest-gap invariants:

| Case | Chain | Validates |
|---|---|---|
| **Ronin Bridge / Lazarus** (2022) | EVM | facts ingest faithfully; the free OFAC pillar independently reproduces the Treasury designation (names Lazarus Group); honest gap — NO fabricated anchor→mixer (Tornado Cash) linkage. |
| **Colonial Pipeline / DarkSide** (2021) | BTC | the clean-room **Invariant #5** test — ransom→seizure flow stored as `tx_input`/`tx_output` facts only (0 transfers); the linkage exists only as `basis='fifo'` trace claims. |
| **Bitfinex 2016 → 2022 seizure** | BTC | **co-spend clustering at scale** — a 166-address seizure consolidation resolves to one cluster, provenance-tracked. |
| **Hydra / Garantex** (OFAC 2022) | BTC | the first **positive GraphSense attribution** + **multi-source never-merge (Inv #4)** — a dual-listed address carries a GraphSense entity *and* an OFAC sanction side-by-side, never collapsed. |
| **Whirlpool CoinJoin** | BTC | **CoinJoin detection** — a real 5×5 Whirlpool mix is flagged `possible-coinjoin` (an ordinary tx is not); the trace treats the mix as an honest deconfusion boundary, never asserting a 1:1 through-link. |

---

## 13. Repository layout

```
backend/app/
  main.py                  FastAPI app (/health, /api/graph, /api/view, /api/graph/expand, /api/cases*,
                           /api/dialog/pick) + orchestrator wiring
  config.py  secrets.py    settings + finality thresholds; OS-keyring secret access
  app_paths.py             central path resolution — bundle_dir/resource_path (read-only bundled assets) vs
                           user_data_dir/cases_root/settings_path/logs_dir (writable); frozen-safe (P7)
  runtime.py               frozen-runtime config (P7): TLS via bundled certifi + OS keyring backend select
  db/                      connection (WAL + FK pragmas), yoyo migration runner (closes its own handle),
                           repository, shared-cache
  migrations/              0001..0009 raw-SQL forward-only migrations
  models/                  canonical Pydantic shapes (Family A/B/C + provenance)
  connectors/              base.py (+ etherscan/esplora/defillama/chainalysis/bitquery/arkham/misttrack/oklink),
                           imports/ (base/arkham/graphsense/ofac), registry.py (paid gating)
  normalization/           canonical.py, finality.py, reconcile.py, valuation_math.py, *_adapter.py
  provenance/atomic.py     the one atomic fact+source_query writer
  services/                orchestrator, valuation, entities, entity_display, claims_display, tracing,
                           finality, investigator, reporting, report_render, export, exhibits, graph,
                           graph_view, cases (runtime active-case + new/open/import), case_registry, dialogs,
                           settings_store (paid-enable + offline + cases-folder overlay, persisted)
  audits/                  runner + checks/ (the 10 invariant audits) + baselines
backend/tests/             unit/ contract/ integration/ property/ + cassettes/ + fixtures/
frontend/                  React 19 + Cytoscape.js (Vite) — investigation graph + claims side-panel;
                           CasePicker.tsx + cases.ts = the case-management entry screen (new/open/import/recent);
                           SettingsPanel.tsx + settings.ts = connectors/keys/cases-folder/offline (write-only keys);
                           ThemeCustomize.tsx = the Custom-preset color editor (Dark/Light/Custom switcher in App header);
                           src/theme/ = the named color-token catalog, 4 themes (Custom/Dark/Light + print-light report);
                           size-aware layout (cose small / fcose large). API: /api/graph, /api/view, /api/cases*, /api/settings*
docs/                      overview, schema, connectors, algorithms, testing, phases/, findings/, validation/
scripts/                   setup, dev_run, launch (pywebview), report, export, set_key, clean
Makefile  pyproject.toml  CLAUDE.md (the contract)  docs/BUILD_INDEX.md (build map)
```

---

## 14. For developers & LLMs — orientation

- **Start with `CLAUDE.md`** (the 8 invariants — the contract; if code violates one, it's a bug regardless
  of tests). Then this README §4–§9, then `docs/schema.md` and `docs/connectors.md`.
- **The golden rule:** a fact/claim and its `source_query` are written in one transaction
  (`write_with_provenance`). Connectors return canonical records; adapters do the mapping; the repository
  does idempotent upserts; nothing downstream knows a source's native shape.
- **Adding a connector:** subclass `BaseHttpConnector` (API) or `ImportConnector` (file); write a pure
  `*_adapter.py`; write a `source_query` per call (even on empty results); add a contract test (cassette)
  + a live-drift entry; if paid, add a `config.py` flag + register in `connectors/registry.py` (key-gated).
- **Adding a schema change:** a new forward-only numbered migration, bump `schema_version`, and keep the
  idempotency-audit natural key in sync.
- **Definition of Done:** `make test && make audit && make smoke` all green, with a test that *fails* if a
  touched invariant breaks.

---

## 15. License

Copyright (C) 2026 Troy Folmer

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU
General Public License** as published by the Free Software Foundation, either **version 3** of the License,
or (at your option) any later version. It is distributed in the hope that it will be useful, but **WITHOUT
ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the full text in [`LICENSE`](LICENSE).

**Why GPL-3.0 (not a permissive license):** two **runtime** dependencies are copyleft — **python-igraph**
(GPL-2.0-or-later; the Leiden community overlay) and **cytoscape-svg** (GPL-3.0; the SVG exhibit export).
GPL-3.0-or-later is the smallest license that combines both cleanly; every other dependency is permissive
(MIT / BSD / Apache-2.0 / MPL-2.0 / PSF) and GPLv3-compatible, and there are no GPLv2-only or proprietary
runtime deps. The full third-party breakdown — including build-time-only tools (PyInstaller's GPL-2.0
bootloader carries the standard exception permitting any-license output) — is in
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md). The bundled OFAC SDN / GraphSense intel snapshots are
openly published attribution data (see their file headers).
