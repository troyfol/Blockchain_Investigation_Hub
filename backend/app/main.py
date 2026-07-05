"""FastAPI app entrypoint (phase_00, extended in phase_04 with the graph read API).

The graph endpoints serve the paradigm-agnostic read model (services/graph.py) over a case DB,
and a bounded-expansion endpoint pulls more on-chain data via the (chain-aware) orchestrator.
Single-user/local: one configured case DB (``BIH_CASE_DB``, default ``cases/dev/case.db``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Literal

import tempfile

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import get_settings
from .connectors.base import ConnectorError
from .db.connection import get_connection
from .runtime import configure_frozen_runtime
from .services import investigator
from .services.graph import bound_subgraph, build_graph
from .services.orchestrator import NoConnectorError, Orchestrator

logger = logging.getLogger("bih.api")

# P7: configure TLS (bundled certifi CA) + the OS keyring backend for the frozen app. Cheap + safe in
# source mode (TLS env setdefault; keyring is a no-op unless frozen). Runs on import so it applies whether
# the server is launched via the desktop launcher or uvicorn directly.
configure_frozen_runtime()

app = FastAPI(title="Blockchain Investigation Hub", version="0.0.0")

# R6 Batch 2 (SEC-02/SEC-04): reject non-loopback Host (DNS-rebinding) + cross-origin state-changing
# requests (CSRF). The API is single-user/local with no auth, so request-provenance is its only defense
# against a hostile browser page.
from .middleware import RequestProvenanceMiddleware  # noqa: E402

app.add_middleware(RequestProvenanceMiddleware)


# R6 Batch 5 — the "never a raw 500 / no key-bearing traceback" error boundary (SEC-03 / RES-02).
# Targeted handlers only (no blanket `Exception` catch-all, which would mask real bugs + change test
# semantics); the key-leak vector itself is closed at the source in `connectors/base.py` (4xx →
# sanitized UpstreamError). These are the defense-in-depth net for any route that doesn't catch locally.

@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_operational_handler(request: Request, exc: sqlite3.OperationalError):
    """RES-02: a write that lost the WAL `busy_timeout` race raises `database is locked` — return a clean,
    retryable 503 (never a raw 500 + traceback). Any other OperationalError → a generic sanitized 500."""
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        logger.warning("db busy on %s %s — returning 503", request.method, request.url.path)
        return JSONResponse(status_code=503, content={"detail": "the case database is busy — retry in a moment"},
                            headers={"Retry-After": "1"})
    logger.error("sqlite OperationalError on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "a database error occurred"})


@app.exception_handler(httpx.HTTPStatusError)
async def _httpx_status_handler(request: Request, exc: httpx.HTTPStatusError):
    """SEC-03 backstop: an httpx.HTTPStatusError (whose message embeds the key-bearing request URL) must
    NEVER reach the default 500 logger. Log only the status + type, respond with a clean 502."""
    code = exc.response.status_code if exc.response is not None else "?"
    logger.warning("upstream HTTP %s on %s %s (sanitized)", code, request.method, request.url.path)
    return JSONResponse(status_code=502, content={"detail": "an upstream data source returned an error"})


@app.exception_handler(ConnectorError)
async def _connector_error_handler(request: Request, exc: ConnectorError):
    """Defense-in-depth: a sanitized ConnectorError/UpstreamError a route didn't catch stays a clean 502
    (its message is already key-free), not a 500."""
    logger.warning("connector error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=502, content={"detail": "an upstream data source failed — try again"})


# --- dependencies --------------------------------------------------------------------------

def get_case_db_path() -> str | None:
    """The ACTIVE case's DB path (mutable at runtime — set by the case picker, P4), or ``None`` if no
    case is active (the entry screen / empty state). Falls back through ``BIH_CASE_DB`` and the
    registry's last-opened inside ``cases.active_case_path``."""
    from .services.cases import active_case_path

    return active_case_path()


def get_case_conn(path: str | None = Depends(get_case_db_path)):
    if not path or not Path(path).exists():
        raise HTTPException(status_code=503,
                            detail="no active case — pick or create one (run `make migrate` for the CLI)")
    conn = get_connection(path, create_parents=False)
    migrated = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_value_movement'").fetchone()
    if not migrated:
        conn.close()
        raise HTTPException(status_code=503, detail=f"case DB {path!r} is not migrated; run `make migrate`")
    try:
        yield conn
    finally:
        conn.close()


def get_orchestrator() -> Orchestrator:
    """Build the orchestrator from configured connectors (keys from the OS keyring)."""
    from .connectors.esplora import EsploraConnector
    from .connectors.etherscan import EtherscanConnector
    from .secrets import get_secret

    settings = get_settings()
    connectors: list = []
    if settings.etherscan_enabled:
        key = get_secret("etherscan")
        if key:
            connectors.append(EtherscanConnector(api_key=key, settings=settings))
        else:
            # No key -> do NOT append a doomed keyless connector (it would make a guaranteed-to-fail
            # network call). EVM chains then have no fact connector, and /api/graph/expand returns the
            # clear "add a free Etherscan key in Settings" guidance instead of a raw upstream error.
            logger.info("Etherscan enabled but no API key in keyring — EVM ingest needs a free key "
                        "(set it in Settings -> Connectors, or secrets.set_secret('etherscan', ...)).")
    if settings.esplora_enabled:
        connectors.append(EsploraConnector(settings=settings))
    # Optional PAID fact connectors (e.g. Bitquery) — present only when enabled AND keyed. Appended
    # AFTER the free connectors, so for a chain the free source covers, the free source wins; paid is a
    # fallback (e.g. a chain Etherscan no longer serves). Never blocks the free baseline (Invariant #4).
    from .connectors.registry import available_fact_connectors
    connectors.extend(available_fact_connectors(settings))
    try:
        yield Orchestrator(connectors)
    finally:
        for c in connectors:  # close the per-request httpx clients
            try:
                c.close()
            except Exception:
                pass


# --- routes --------------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    from .connectors.registry import paid_status

    settings = get_settings()
    return {
        "status": "ok",
        "etherscan_enabled": settings.etherscan_enabled,
        "esplora_enabled": settings.esplora_enabled,
        "finality_thresholds": settings.finality_thresholds,
        # Optional paid sources: available only when enabled AND keyed; silently absent otherwise.
        "paid_connectors": paid_status(settings),
    }


@app.get("/api/graph")
def api_graph(address_id: str | None = None, limit: int | None = None,
              conn=Depends(get_case_conn)) -> dict:
    """The FULL heterogeneous graph for the case, projected from v_value_movement (+ tx_input) — with
    OPTIONAL scope/pagination (P25/FN-20) so a LEA-scale case need not return the whole case at once:

    - ``?address_id=<id>`` bounds the projection to that address's NEIGHBOURHOOD (the two O(case) scans are
      constrained to incident rows, reusing ``focus_incident`` — a real DB-scan bound, not just a payload trim).
    - ``?limit=N`` returns at most the N highest-degree nodes as a bounded subgraph, plus a ``meta`` block
      (``total_nodes``/``returned_nodes``/``truncated``).

    With NEITHER param the response is byte-identical to before — the truthful, unbounded projection the
    report renders. For interactive dense-case browsing the app still uses the bounded /api/view."""
    focus = None
    if address_id is not None:
        if not conn.execute("SELECT 1 FROM address WHERE id=?", (address_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"address {address_id!r} is not in this case")
        focus = f"addr:{address_id}"
    graph = build_graph(conn, focus_incident=focus)
    if limit is not None:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be a positive integer")
        graph = bound_subgraph(graph, limit)
    return graph


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


@app.get("/api/view")
def api_view(
    conn=Depends(get_case_conn),
    focus: str | None = None,
    hops: int = 1,
    node_cap: int = 150,
    group_dust: bool = True,
    dust_floor_usd: float = 1.0,
    dust_floor_native: float = 0.001,
    value_floor_usd: float = 0.0,
    edge_kinds: str | None = None,
    only_flagged: bool = False,
    user_dust_usd: float | None = None,
    expand: str | None = None,
    value_basis: str = "usd",
    group_denominations: bool = False,
    show_unverified: bool = False,
    fold_poison: bool = True,
    denom_filters: str | None = None,
    community: bool = False,
) -> dict:
    """A BOUNDED, scale-aware view of the case for the live canvas: focus on one node (seed/anchor by
    default), walk ``hops`` outward capped at ``node_cap``, collapse dust/high-fan-in counterparties into
    expandable summary nodes, and carry ``meta`` for 'displaying N of M (bounded)'. ``value_basis`` (usd|
    native) switches labels/thickness/dust/ordering between USD value-at-time and native units (per-asset).
    ``user_dust_usd`` folds sub-threshold movements into a distinct user_dust bucket (unpriced stay visible
    in USD mode). ``group_denominations`` clusters equal-native-denomination pools (mixer structure).
    Display-only over the real facts — no rows written, every aggregate expandable (Inv #5/#3)."""
    import json as _json

    from .services.graph_view import build_view

    df: dict = {}
    if denom_filters:
        try:
            parsed = _json.loads(denom_filters)
            if isinstance(parsed, dict):  # {asset_symbol: {min: float, fold: float}}
                df = {str(k): {kk: float(vv) for kk, vv in (v or {}).items() if kk in ("min", "fold")}
                      for k, v in parsed.items() if isinstance(v, dict)}
        except (ValueError, TypeError):
            df = {}

    return build_view(
        conn, focus=focus, hops=max(1, hops), node_cap=max(1, node_cap), group_dust=group_dust,
        dust_floor_usd=max(0.0, dust_floor_usd), dust_floor_native=max(0.0, dust_floor_native),
        value_floor_usd=max(0.0, value_floor_usd),
        edge_kinds=_csv(edge_kinds) or None, only_flagged=only_flagged,
        user_dust_usd=(user_dust_usd if user_dust_usd and user_dust_usd > 0 else None),
        expand=tuple(_csv(expand)),
        value_basis=("native" if value_basis == "native" else "usd"),
        group_denominations=group_denominations,
        show_unverified=show_unverified, fold_poison=fold_poison, denom_filters=df,
        community_detect=community)


@app.get("/api/node/{node_id}/summary")
def api_node_summary(node_id: str, conn=Depends(get_case_conn)) -> dict:
    """Value summary + RANKED counterparties for one node (the SidePanel list view): top counterparties
    by USD value-at-time, with in/out split, count, and risk/attribution flags. Over the node's real
    incident facts (not just the focused view)."""
    from .services.graph import build_graph as _bg

    # EFF-01: build only the node's neighborhood (incident movements/inputs), not the whole case — the
    # aggregation below is unchanged, so the summary is identical to the full-graph result, just cheaper.
    g = _bg(conn, focus_incident=node_id)
    nodes = {n["id"]: n for n in g["nodes"]}
    if node_id not in nodes:
        raise HTTPException(status_code=404, detail="node not found")

    agg: dict[str, dict] = {}
    for e in g["edges"]:
        if e.get("kind") == "trace":
            continue
        if e["source"] == node_id or e["target"] == node_id:
            other = e["target"] if e["source"] == node_id else e["source"]
            if other == node_id:
                continue
            direction = "out" if e["source"] == node_id else "in"
            o = nodes.get(other, {})
            d = agg.setdefault(other, {
                "id": other, "label": o.get("label"), "kind": o.get("kind"),
                "address": o.get("address"), "risk_level": o.get("risk_level"),
                "has_attribution": o.get("has_attribution"), "entity_label": o.get("entity_label"),
                "in_usd": 0.0, "out_usd": 0.0, "in_count": 0, "out_count": 0, "usd": 0.0, "count": 0})
            usd = e.get("value_usd") or 0.0
            d[f"{direction}_usd"] += usd
            d[f"{direction}_count"] += 1
            d["usd"] += usd
            d["count"] += 1
    ranked = sorted(agg.values(), key=lambda x: (-x["usd"], -x["count"]))
    for d in ranked:
        d["in_usd"] = round(d["in_usd"], 2) or None
        d["out_usd"] = round(d["out_usd"], 2) or None
        d["usd"] = round(d["usd"], 2) or None
    flagged = [d for d in ranked if d["risk_level"] or d["has_attribution"]]
    return {"node_id": node_id, "label": nodes[node_id].get("label"),
            "val": nodes[node_id].get("val"),
            "counterparties": ranked[:50], "counterparty_total": len(ranked),
            "flagged": flagged[:50]}


# --- case management (entry screen: new / open / import / recent) --------------------------
# The active case is mutable at runtime (services/cases.py): the picker switches it, connections stay
# per-request, and a switch checkpoints the prior case's WAL. These routes do NOT depend on
# get_case_conn — they work with NO case active (that's the whole point of the entry screen).
# Security: opening/importing a case READS it (data, not commands); import VERIFIES the bundle first and
# only opens it when verification passes (or the caller loudly accepts an untrusted bundle).

class NewCaseBody(BaseModel):
    title: str
    location: str | None = None
    template: str | None = None   # P26/FN-22: optional declarative preset id


class OpenCaseBody(BaseModel):
    path: str


class ImportCaseBody(BaseModel):
    path: str
    allow_untrusted: bool = False


class ForgetCaseBody(BaseModel):
    path: str


class DialogPickBody(BaseModel):
    kind: str  # 'casefile' | 'casedb' | 'folder'


@app.get("/api/cases")
def api_cases() -> dict:
    """The Recent list — known cases, most-recent first, pruned of any whose case.db is gone."""
    from .services import case_registry

    return {"cases": case_registry.list_cases()}


@app.get("/api/cases/active")
def api_active_case() -> dict:
    """The active case's metadata (drives the header/title), or ``{"active": null}`` -> show picker."""
    from .services import cases

    return {"active": cases.active_meta()}


@app.get("/api/case_templates")
def api_case_templates() -> dict:
    """The declarative case templates (P26/FN-22) for the New-case picker — each pre-seeds a scenario's
    methodology stub + connectors + a first-ingest bound hint. Read-only; a template is optional."""
    from .services.case_templates import list_templates

    return {"templates": list_templates()}


@app.post("/api/cases/new")
def api_new_case(body: NewCaseBody) -> dict:
    """Create a fresh case (folder + schema + case_meta), register it, and make it active. An optional
    ``template`` (P26/FN-22) pre-seeds the case's methodology + scenario connectors (settings only)."""
    from .services import cases

    try:
        res = cases.new_case(body.title, location=body.location, template=body.template)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not create the case folder: {exc}")
    out = {"ok": True, "created": True, "active": cases.active_meta(), "path": res["path"]}
    if "template" in res:
        out["template"] = res["template"]   # echo the applied preset (default_bounds hint for the first ingest)
    return out


@app.post("/api/cases/open")
def api_open_case(body: OpenCaseBody) -> dict:
    """Open an existing case.db / case folder: validate it's a BIH case, migrate forward, set active.
    Surfaces ``migrated`` so the UI can note a forward migration ran on open."""
    from .services import cases

    from .db import SchemaTooNewError

    try:
        res = cases.open_case(body.path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SchemaTooNewError as exc:  # LOG-03: a case created by a newer app version
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "migrated": res["migrated"], "active": cases.active_meta(), "path": res["path"]}


def _import_result(out: dict) -> dict:
    """Shape the import response so the UI can branch tamper-vs-audit ON DATA, not string-matching.
    TAMPER = the bundle's FILES are wrong (hash mismatch, or a structural self-containment violation =
    altered after sealing): ``manifest_ok``/``self_contained_ok`` false. AUDIT WARNING = every file is
    authentic (hashes match, self-contained) but an invariant audit fails: ``audits_passed`` false with
    the files OK. A fully clean bundle has all three true (and opens with no warning)."""
    from .services import cases

    v = out["verification"]
    manifest = v.get("manifest") or {}
    sc = v.get("self_contained") or {}
    manifest_ok = bool(manifest.get("ok", True))
    self_contained_ok = not (sc.get("attached_databases") or sc.get("fk_violations")
                             or sc.get("missing_referenced_files") or sc.get("unsafe_referenced_paths"))
    audits_passed = bool(sc.get("audits_passed", True))
    return {"ok": out["opened"], "opened": out["opened"], "trusted": out["trusted"],
            "manifest_ok": manifest_ok, "self_contained_ok": self_contained_ok,
            "audits_passed": audits_passed,
            "verification": v,
            "active": cases.active_meta() if out["opened"] else None}


@app.post("/api/cases/import")
def api_import_case(body: ImportCaseBody) -> dict:
    """Import a ``.casefile`` by server-side path (the native-dialog flow). VERIFIES before opening; a
    tampered/unsafe bundle is reported and NOT opened unless ``allow_untrusted`` is explicitly set."""
    from .services import cases

    try:
        out = cases.import_casefile(body.path, allow_untrusted=body.allow_untrusted)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:  # SEC-05: an oversized/unsafe bundle rejected before inflating
        raise HTTPException(status_code=400, detail=str(exc))
    return _import_result(out)


@app.post("/api/cases/import-upload")
async def api_import_case_upload(request: Request, filename: str = "imported.casefile",
                                 allow_untrusted: bool = False) -> dict:
    """Import a ``.casefile`` uploaded as the raw request body (the dev/browser ``<input type=file>``
    flow — no multipart dependency). Same verify-before-open gate as the path-based import."""
    from .services import cases

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload — choose a .casefile to import")
    tmp_dir = Path(tempfile.mkdtemp(prefix="bih_upload_"))
    tmp = tmp_dir / (Path(filename).name or "imported.casefile")
    tmp.write_bytes(data)
    try:
        out = cases.import_casefile(tmp, allow_untrusted=allow_untrusted)
    except ValueError as exc:  # SEC-05: an oversized/unsafe bundle rejected before inflating
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        try:
            tmp.unlink()
            tmp_dir.rmdir()
        except OSError:
            pass
    return _import_result(out)


@app.get("/api/cases/sample")
def api_sample_case() -> dict:
    """Whether this build ships a bundled first-run sample case (P39). The CasePicker shows the
    'Explore the sample case' affordance only when one is available."""
    from .services import cases

    return {"available": cases.sample_casefile_path() is not None}


@app.post("/api/cases/import-sample")
def api_import_sample_case() -> dict:
    """Import + open the bundled first-run sample case (P39 'Explore the sample case') — same verify gate
    and ImportResult shape as ``/api/cases/import``. 404 when this build ships no sample."""
    from .services import cases

    try:
        out = cases.import_sample_case()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:  # SEC-05: an unsafe bundle rejected before inflating
        raise HTTPException(status_code=400, detail=str(exc))
    return _import_result(out)


@app.post("/api/cases/forget")
def api_forget_case(body: ForgetCaseBody) -> dict:
    """Remove a case from the Recent list WITHOUT deleting it on disk ('remove from list' != 'delete')."""
    from .services import case_registry

    removed = case_registry.forget(body.path)
    return {"ok": True, "removed": removed, "cases": case_registry.list_cases()}


@app.post("/api/dialog/pick")
def api_dialog_pick(body: DialogPickBody) -> dict:
    """Open the native OS file dialog (windowed app only). Returns selected path(s); ``501`` in
    dev/browser mode where no pywebview window is registered (the UI falls back to upload + path field)."""
    from .services import cases, dialogs

    if body.kind not in dialogs.DIALOG_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown dialog kind {body.kind!r}")
    window = cases.get_native_window()
    if window is None:
        raise HTTPException(status_code=501,
                            detail="native file dialog unavailable (browser/dev mode) — "
                                   "use the file upload or the path field")
    try:
        paths = dialogs.pick_path(window, body.kind)
    except Exception as exc:  # a GUI backend hiccup must not 500 the API
        raise HTTPException(status_code=500, detail=f"file dialog failed: {exc}")
    return {"paths": paths}


# --- settings (connectors · keys->keyring · cases folder · offline) ------------------------
# Operator-tunable settings (P5). CREDENTIAL BOUNDARY (non-negotiable): API keys are written DIRECTLY
# to the OS keyring and are NEVER read back to the UI, returned by any endpoint, or logged — every
# payload here exposes only key PRESENCE (a bool), never a value. The free pillars are always-on
# (Invariant #4 — never block the free baseline); only the paid connectors toggle + take a key.

# Free pillars shown as always-on (display-only): the free baseline can't be disabled.
FREE_CONNECTORS = [
    {"name": "etherscan", "label": "Etherscan (EVM facts)", "kind": "fact"},
    {"name": "esplora", "label": "Esplora (Bitcoin facts)", "kind": "fact"},
    {"name": "defillama", "label": "DeFiLlama (valuation)", "kind": "valuation"},
    {"name": "graphsense", "label": "GraphSense (attribution import)", "kind": "intel"},
    {"name": "ofac", "label": "OFAC SDN (sanctions import)", "kind": "intel"},
    {"name": "chainalysis-free", "label": "Chainalysis sanctions (free)", "kind": "intel"},
]

# Free pillars that REQUIRE a key to FUNCTION get the same write-only keyring field as the paid
# connectors (but stay always-on — no enable toggle). Maps the connector NAME -> its keyring slot.
# Etherscan is load-bearing: EVM fact ingest reads ``get_secret("etherscan")``. Chainalysis's free
# sanctions screening also needs a (free) key. The other free pillars (Esplora/DeFiLlama/GraphSense/OFAC)
# need none. Same credential boundary as paid keys: write-only, never returned/logged.
KEYABLE_FREE = {
    "etherscan": "etherscan",
    "chainalysis-free": "chainalysis_api_key",
}


def _keyring_slot_for(connector: str) -> str | None:
    """Resolve a connector NAME to its OS-keyring slot, for BOTH paid connectors (registry) and the
    keyable free pillars (Etherscan etc.). ``None`` if the name takes no API key."""
    from .connectors.registry import spec_by_name

    spec = spec_by_name(connector)
    if spec is not None:
        return spec["keyring"]
    return KEYABLE_FREE.get(connector)


def _free_payload(c: dict) -> dict:
    """A free pillar's status for the UI: always-on, plus key PRESENCE for the keyable ones (Etherscan).
    Never returns a key value (credential boundary)."""
    from .secrets import get_secret

    slot = KEYABLE_FREE.get(c["name"])
    requires_key = slot is not None
    return {**c, "always_on": True, "requires_key": requires_key,
            "key_present": bool(get_secret(slot)) if requires_key else False}


class ConnectorToggle(BaseModel):
    name: str
    enabled: bool


class SettingsPatch(BaseModel):
    offline: bool | None = None
    cases_folder: str | None = None
    connector: ConnectorToggle | None = None


class KeyBody(BaseModel):
    key: str


def _paid_payload(p: dict) -> dict:
    """A paid connector's status for the UI — presence/absence only, NEVER a key value."""
    status = "available" if p["available"] else ("needs-key" if p["enabled"] else "disabled")
    return {"name": p["name"], "kind": p["kind"], "capabilities": p["capabilities"],
            "enabled": p["enabled"], "key_present": p["has_key"], "available": p["available"],
            "status": status}


def _settings_payload() -> dict:
    from .connectors.registry import paid_status
    from .secrets import keyring_status
    from .services import settings_store

    settings = get_settings()
    return {
        "connectors": {
            "free": [_free_payload(c) for c in FREE_CONNECTORS],
            "paid": [_paid_payload(p) for p in paid_status(settings)],
        },
        "cases_folder": str(settings_store.cases_root()),
        "offline": settings_store.is_offline(),
        "evm_chains": _evm_chains(),  # the chains the add-address control can ingest (no UI drift)
        "intel": _intel_snapshot_info(),  # P8.7 — OFAC/GraphSense snapshot dates + override state
        "keyring": keyring_status(),  # backend availability + plaintext-active flag (no secrets)
    }


def _intel_snapshot_info() -> dict:
    from .services.intel import snapshot_info

    return snapshot_info()


def _evm_chains() -> list[str]:
    """The EVM chains the app can ingest, sourced from the Etherscan V2 connector's own map so the UI
    never offers a chain that 500s at ingest time."""
    from .connectors.etherscan import CHAIN_TO_CHAINID

    return sorted(CHAIN_TO_CHAINID)


@app.get("/api/settings")
def api_settings() -> dict:
    """Connector status (free = always-on; paid = enabled/key_present/available), the cases folder, the
    offline flag, and keyring backend availability. NEVER returns a key value."""
    return _settings_payload()


@app.patch("/api/settings")
def api_patch_settings(body: SettingsPatch) -> dict:
    """Toggle a paid connector's enabled flag, set the cases folder, and/or set offline mode."""
    from .connectors.registry import spec_by_name
    from .services import settings_store

    if body.offline is not None:
        settings_store.set_offline(body.offline)
    if body.cases_folder is not None:
        folder = body.cases_folder.strip()
        if not folder:
            raise HTTPException(status_code=400, detail="cases folder must be a non-empty path")
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"could not use that folder: {exc}")
        settings_store.set_cases_root(folder)
    if body.connector is not None:
        if spec_by_name(body.connector.name) is None:
            raise HTTPException(status_code=400, detail=f"unknown paid connector {body.connector.name!r}")
        settings_store.set_paid_enabled(body.connector.name, body.connector.enabled)
    return _settings_payload()


@app.post("/api/settings/keys/{connector}")
def api_set_key(connector: str, body: KeyBody) -> dict:
    """Write a connector's API key STRAIGHT to the OS keyring — for paid connectors AND the keyable free
    pillars (Etherscan, needed for EVM ingest). Returns only success + key_present; the key value is never
    echoed, returned, or logged (credential boundary)."""
    from . import secrets

    slot = _keyring_slot_for(connector)
    if slot is None:
        raise HTTPException(status_code=400, detail=f"connector {connector!r} does not take an API key")
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key must be non-empty")
    ks = secrets.keyring_status()
    if not ks["available"]:
        raise HTTPException(status_code=503, detail=ks["message"] or "no OS keyring backend available")
    try:
        secrets.set_secret(slot, key)
    except Exception as exc:  # backend hiccup — clear message, never leak the key
        raise HTTPException(status_code=503, detail=f"could not store the key in the OS keyring: {exc}")
    return {"ok": True, "connector": connector, "key_present": True}


@app.delete("/api/settings/keys/{connector}")
def api_delete_key(connector: str) -> dict:
    """Clear a connector's API key from the OS keyring (paid or keyable-free; no error if absent)."""
    from . import secrets

    slot = _keyring_slot_for(connector)
    if slot is None:
        raise HTTPException(status_code=400, detail=f"connector {connector!r} does not take an API key")
    secrets.delete_secret(slot)
    return {"ok": True, "connector": connector, "key_present": False}


# --- investigator annotations (durable notes; green outline) -------------------------------

def _resolve_node_target(node_id: str) -> tuple[str | None, str | None]:
    """A graph node id -> (annotation target_type, target_id). Only address / transaction nodes carry
    annotations in the UI (aggregates/groups/external are view artifacts, not durable objects)."""
    if node_id.startswith("addr:"):
        return "address", node_id[len("addr:"):]
    if node_id.startswith("tx:"):
        return "transaction", node_id[len("tx:"):]
    return None, None


class AnnotationBody(BaseModel):
    content: str


class LabelBody(BaseModel):
    label: str


@app.get("/api/node/{node_id}/annotations")
def api_list_annotations(node_id: str, conn=Depends(get_case_conn)) -> dict:
    tt, tid = _resolve_node_target(node_id)
    if tt is None:
        raise HTTPException(status_code=400, detail="annotations are only supported on address/tx nodes")
    return {"annotations": investigator.list_annotations(conn, target_type=tt, target_id=tid)}


@app.post("/api/node/{node_id}/annotations")
def api_add_annotation(node_id: str, body: AnnotationBody, conn=Depends(get_case_conn)) -> dict:
    """Add a durable investigator note (Family C claim, never a fact — Inv #4/#5). Re-renders with a green
    outline on the target. Returns the annotation list; the client reloads the view for the outline."""
    tt, tid = _resolve_node_target(node_id)
    if tt is None:
        raise HTTPException(status_code=400, detail="annotations are only supported on address/tx nodes")
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="annotation content must be non-empty")
    try:
        investigator.add_annotation(conn, target_type=tt, target_id=tid, content=content)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "annotations": investigator.list_annotations(conn, target_type=tt, target_id=tid)}


# --- generic target annotate / relabel: any annotatable / relabelable object ----------------
# Addresses + transactions are graph NODES; transfers + tx_outputs are FLOWS (edges). The frontend
# resolves a selected node OR edge to a (target_type, target_id) and uses these one set of endpoints for
# every object — so renaming + annotating works uniformly for nodes and flows (P1.5 follow-up A2/A3).
# Durable investigator inputs (Family C claims, never facts — Inv #4/#5); the underlying facts are
# untouched. The annotation/label services validate the target_type against their own allowed sets.

@app.get("/api/target/{target_type}/{target_id}/annotations")
def api_target_annotations(target_type: str, target_id: str, conn=Depends(get_case_conn)) -> dict:
    from .models.investigator import ANNOTATION_TARGET_TYPES

    if target_type not in ANNOTATION_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"cannot annotate a {target_type!r}")
    return {"annotations": investigator.list_annotations(conn, target_type=target_type, target_id=target_id)}


@app.post("/api/target/{target_type}/{target_id}/annotations")
def api_target_add_annotation(target_type: str, target_id: str, body: AnnotationBody,
                              conn=Depends(get_case_conn)) -> dict:
    from .models.investigator import ANNOTATION_TARGET_TYPES

    if target_type not in ANNOTATION_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"cannot annotate a {target_type!r}")
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="annotation content must be non-empty")
    try:
        investigator.add_annotation(conn, target_type=target_type, target_id=target_id, content=content)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True,
            "annotations": investigator.list_annotations(conn, target_type=target_type, target_id=target_id)}


@app.patch("/api/annotations/{annotation_id}")
def api_edit_annotation(annotation_id: str, body: AnnotationBody, conn=Depends(get_case_conn)) -> dict:
    """Edit an annotation's text (Family C; editable like a finding). Returns the target's refreshed note
    list so the side panel + Findings panel update; the client reloads the view for the green outline."""
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="annotation content must be non-empty")
    try:
        tt, tid = investigator.update_annotation(conn, annotation_id=annotation_id, content=content)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "target_type": tt, "target_id": tid,
            "annotations": investigator.list_annotations(conn, target_type=tt, target_id=tid)}


@app.delete("/api/annotations/{annotation_id}")
def api_delete_annotation(annotation_id: str, conn=Depends(get_case_conn)) -> dict:
    """Delete an annotation. Returns the target's refreshed note list; deleting the LAST note on a target
    clears its green outline/glow on the next view reload (the read-model recomputes has_annotation)."""
    try:
        tt, tid = investigator.delete_annotation(conn, annotation_id=annotation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "target_type": tt, "target_id": tid,
            "annotations": investigator.list_annotations(conn, target_type=tt, target_id=tid)}


@app.post("/api/target/{target_type}/{target_id}/label")
def api_target_set_label(target_type: str, target_id: str, body: LabelBody,
                         conn=Depends(get_case_conn)) -> dict:
    """Set an investigator display-label override on any relabelable object — an address, a transaction,
    or a flow (transfer / tx_output). A CLAIM, not a fact (Inv #5/#6): it takes display precedence on the
    canvas + report but leaves the underlying object's facts untouched. Returns the rebuilt graph so the
    canvas updates immediately."""
    from .models.investigator import INVESTIGATOR_LABEL_TARGET_TYPES

    if target_type not in INVESTIGATOR_LABEL_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"cannot relabel a {target_type!r}")
    try:
        investigator.set_label(conn, target_type=target_type, target_id=target_id, label=body.label)
    except ValueError as exc:
        # an unknown target id (poly ref would dangle) vs a bad/empty label
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc))
    return {"ok": True}  # EFF-01: the client refetches /api/view; never build a full graph it discards


# --- Findings & Notes composer -------------------------------------------------------------

class FindingRefBody(BaseModel):
    ref_type: str
    ref_id: str
    note: str | None = None


class FindingBody(BaseModel):
    statement: str
    assessment: str | None = None
    refs: list[FindingRefBody] | None = None


class FindingUpdateBody(BaseModel):
    statement: str
    assessment: str | None = None


@app.get("/api/investigator/notes")
def api_investigator_notes(conn=Depends(get_case_conn)) -> dict:
    """Every investigator INPUT (annotations + label overrides + tags) grouped by target — the Findings &
    Notes panel source (with jump-to node ids)."""
    return {"notes": investigator.collect_notes(conn)}


@app.get("/api/findings")
def api_list_findings(conn=Depends(get_case_conn)) -> dict:
    return {"findings": investigator.list_findings(conn)}


@app.post("/api/findings")
def api_create_finding(body: FindingBody, conn=Depends(get_case_conn)) -> dict:
    """Compose a finding = statement + optional assessment + finding_ref targets. Editable until reported;
    flows to the report's Findings section (Inv #3 provenance / #4 never a fact)."""
    statement = (body.statement or "").strip()
    if not statement:
        raise HTTPException(status_code=400, detail="finding statement must be non-empty")
    fid = investigator.create_finding(conn, statement=statement, assessment=body.assessment)
    try:
        for r in body.refs or []:
            investigator.add_finding_ref(conn, finding_id=fid, ref_type=r.ref_type, ref_id=r.ref_id,
                                         note=r.note)
    except ValueError as exc:  # bad ref -> roll back the just-created finding (no orphan)
        investigator.delete_finding(conn, finding_id=fid)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "finding_id": fid, "findings": investigator.list_findings(conn)}


@app.patch("/api/findings/{finding_id}")
def api_update_finding(finding_id: str, body: FindingUpdateBody, conn=Depends(get_case_conn)) -> dict:
    try:
        investigator.update_finding(conn, finding_id=finding_id, statement=body.statement,
                                    assessment=body.assessment)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc))
    return {"ok": True, "findings": investigator.list_findings(conn)}


@app.delete("/api/findings/{finding_id}")
def api_delete_finding(finding_id: str, conn=Depends(get_case_conn)) -> dict:
    try:
        investigator.delete_finding(conn, finding_id=finding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "findings": investigator.list_findings(conn)}


@app.post("/api/findings/{finding_id}/refs")
def api_add_finding_ref(finding_id: str, body: FindingRefBody, conn=Depends(get_case_conn)) -> dict:
    try:
        investigator.add_finding_ref(conn, finding_id=finding_id, ref_type=body.ref_type,
                                     ref_id=body.ref_id, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "findings": investigator.list_findings(conn)}


@app.delete("/api/findings/refs/{ref_id}")
def api_remove_finding_ref(ref_id: str, conn=Depends(get_case_conn)) -> dict:
    investigator.remove_finding_ref(conn, ref_id=ref_id)
    return {"ok": True, "findings": investigator.list_findings(conn)}


@app.get("/api/address/{address_id}/claims")
def api_address_claims(address_id: str, conn=Depends(get_case_conn)) -> dict:
    """All sourced claims for an address, grouped by source and kept side-by-side — NEVER collapsed
    into one synthesized label/score (Invariant #4). Returns `attributions_by_source`,
    `risks_by_source`, and the address's `entities` (e.g. a GraphSense actor entity)."""
    from .services.claims_display import address_claims

    if conn.execute("SELECT 1 FROM address WHERE id=?", (address_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="address not found")
    claims = address_claims(conn, address_id)
    entities = [dict(r) for r in conn.execute(
        """SELECT e.name, e.entity_type, e.origin, m.source, m.method, m.confidence, m.flags,
                  m.source_query_id
           FROM entity_membership m JOIN entity e ON e.id=m.entity_id
           WHERE m.address_id=? ORDER BY m.source, e.name""", (address_id,)).fetchall()]
    return {**claims, "entities": entities}


@app.get("/api/movement/{subject_id}/valuations")
def api_movement_valuations(subject_id: str, conn=Depends(get_case_conn)) -> dict:
    """Every valuation for a movement (transfer / tx_output), grouped by source and kept side-by-side —
    never collapsed into one number or an average (Invariant #4). Powers the SidePanel's per-source value
    stack on a contested movement (FN-03). An unvalued movement returns an empty map (an honest gap)."""
    from .services.valuation_display import movement_valuations

    return movement_valuations(conn, subject_id)


@app.get("/api/disagreements")
def api_disagreements(conn=Depends(get_case_conn)) -> dict:
    """Every subject where sources DISAGREE (attribution label/category, risk category, or a movement's
    valuation), each with the sources' claims side-by-side + the fields that differ + a node to navigate
    to. The tool NEVER emits a winner or a merged/averaged value (Invariant #4) — adjudication is an
    explicit investigator finding."""
    from .services.disagreements import find_disagreements

    return {"disagreements": find_disagreements(conn)}


@app.get("/api/activity")
def api_activity(conn=Depends(get_case_conn)) -> dict:
    """The case activity timeline (P24/FN-14): one time-ordered log of every timestamped event — data
    fetches (each `source_query`, covering ingest + valuation/enrichment) and the investigator's
    constructions (traces, findings, annotations, tags, trace edits, bridge links, exhibits, reports).
    Read-only aggregation, deterministically ordered; feeds the chain-of-custody narrative."""
    from .services.activity import case_activity

    return {"activity": case_activity(conn)}


@app.get("/api/source_query/{source_query_id}")
def api_source_query(source_query_id: str, conn=Depends(get_case_conn)) -> dict:
    """The provenance drill-through behind every displayed fact/claim (FN-01, Invariant #3): the exact
    query that produced it — connector, capability, endpoint, params/bounds, retrieval time, and the
    raw-response hash. Read-only; 404 if the id is unknown in this case."""
    from .services.provenance_display import source_query as get_source_query

    sq = get_source_query(conn, source_query_id)
    if sq is None:
        raise HTTPException(status_code=404, detail="source_query not found")
    return sq


@app.post("/api/address/{address_id}/label")
def api_set_address_label(address_id: str, body: LabelBody, conn=Depends(get_case_conn)) -> dict:
    """Set an investigator display-label override on an address node (feature 4). A CLAIM, not a fact:
    it takes display precedence on the graph but leaves the address + its facts untouched (Inv #5/#6).
    Returns the rebuilt graph so the canvas updates immediately."""
    from .services.investigator import set_label

    if conn.execute("SELECT 1 FROM address WHERE id=?", (address_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="address not found")
    try:
        set_label(conn, target_type="address", target_id=address_id, label=body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}  # EFF-01: the client refetches /api/view; never build a full graph it discards


@app.get("/api/traces")
def api_traces(conn=Depends(get_case_conn)) -> dict:
    """List the case's traces with their DISPLAY name (the investigator's latest custom label overrides
    the trace's original name), so the UI can show + rename paths (feature 5)."""
    from .services.investigator import current_labels

    custom = current_labels(conn, "trace")
    out = []
    for t in conn.execute(
            "SELECT id, name, description FROM trace t "
            "WHERE NOT EXISTS (SELECT 1 FROM trace_retraction r WHERE r.trace_id=t.id) "  # v1.3.1: hide soft-deleted traces
            "ORDER BY t.created_at, t.id").fetchall():
        # FN-04: counts reflect the EFFECTIVE trace — retracted edges/links are excluded (their rows persist).
        btc = conn.execute(
            "SELECT COUNT(*) FROM trace_btc_link l WHERE l.trace_id=? "
            "AND NOT EXISTS (SELECT 1 FROM trace_btc_link_retraction r WHERE r.trace_btc_link_id=l.id)",
            (t["id"],)).fetchone()[0]
        ev = conn.execute(
            "SELECT COUNT(*) FROM trace_transfer tt WHERE tt.trace_id=? "
            "AND NOT EXISTS (SELECT 1 FROM trace_transfer_retraction r WHERE r.trace_transfer_id=tt.id)",
            (t["id"],)).fetchone()[0]
        out.append({"id": t["id"], "name": custom.get(t["id"]) or t["name"],
                    "original_name": t["name"], "description": t["description"],
                    "btc_link_count": btc, "transfer_count": ev, "custom_label": t["id"] in custom})
    return {"traces": out}


# --- trace CONSTRUCTION (R6 Batch 9 / LOG-04) — build + populate a trace through the API ------------
# Traces are investigator constructions (Family C, provenance-exempt by schema design). These endpoints
# make the trace-construction service reachable so the shipped app can actually BUILD a trace (list +
# rename + render already existed); previously `/api/traces` was permanently empty. FIFO/manual-link
# writers are insert-once (LOG-07), so re-running is safe.

class NewTraceBody(BaseModel):
    name: str
    description: str | None = None


class TraceTransferBody(BaseModel):
    transfer_id: str
    ordering: int | None = None
    note: str | None = None


class TraceFifoBody(BaseModel):
    transaction_id: str
    ordering_start: int = 0


class TraceLinkBody(BaseModel):
    transaction_id: str
    source_output_id: str
    dest_output_id: str
    confidence: float | None = None
    ordering: int | None = None
    note: str | None = None


class RetractBody(BaseModel):
    reason: str


class BridgeLinkBody(BaseModel):
    src_subject_type: Literal["transfer", "tx_output"]
    src_subject_id: str
    dst_subject_type: Literal["transfer", "tx_output"]
    dst_subject_id: str
    confidence: float | None = None
    note: str | None = None


def _require_trace(conn, trace_id: str) -> None:
    if conn.execute("SELECT 1 FROM trace WHERE id=?", (trace_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="trace not found")


@app.post("/api/trace")
def api_create_trace(body: NewTraceBody, conn=Depends(get_case_conn)) -> dict:
    """Create a named trace (empty). Populate it via /transfer (EVM edge), /fifo (BTC apportionment),
    or /link (investigator BTC link)."""
    from .services.tracing import create_trace

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="a trace needs a non-empty name")
    trace_id = create_trace(conn, name=name, description=body.description)
    return {"ok": True, "trace_id": trace_id}


@app.post("/api/trace/{trace_id}/transfer")
def api_trace_add_transfer(trace_id: str, body: TraceTransferBody, conn=Depends(get_case_conn)) -> dict:
    """Add an EVM edge (a real `transfer` fact) to a trace. Insert-once on (trace, transfer)."""
    from .services.tracing import add_trace_transfer

    _require_trace(conn, trace_id)
    try:
        tt_id = add_trace_transfer(conn, trace_id=trace_id, transfer_id=body.transfer_id,
                                   ordering=body.ordering, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "trace_transfer_id": tt_id}


@app.post("/api/trace/{trace_id}/fifo")
def api_trace_fifo(trace_id: str, body: TraceFifoBody, conn=Depends(get_case_conn)) -> dict:
    """Apportion one Bitcoin transaction by the FIFO convention into `trace_btc_link(basis='fifo')` rows
    (a labeled convention, never ground-truth flow). Re-running is a no-op (LOG-07)."""
    from .services.tracing import fifo_trace_transaction

    _require_trace(conn, trace_id)
    try:
        res = fifo_trace_transaction(conn, trace_id=trace_id, transaction_id=body.transaction_id,
                                     ordering_start=body.ordering_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **res}


@app.post("/api/trace/{trace_id}/link")
def api_trace_add_link(trace_id: str, body: TraceLinkBody, conn=Depends(get_case_conn)) -> dict:
    """Add an investigator-asserted Bitcoin link (`basis='investigator'`) within one transaction."""
    from .services.tracing import add_manual_link

    _require_trace(conn, trace_id)
    try:
        link_id = add_manual_link(conn, trace_id=trace_id, transaction_id=body.transaction_id,
                                  source_output_id=body.source_output_id,
                                  dest_output_id=body.dest_output_id, confidence=body.confidence,
                                  ordering=body.ordering, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "trace_btc_link_id": link_id}


@app.get("/api/transaction/{tx_id}/btc_link_candidates")
def api_btc_link_candidates(tx_id: str, conn=Depends(get_case_conn)) -> dict:
    """UX-06: the legal endpoints for a manual within-tx BTC link on this transaction — `sources` are the
    prev-outputs the tx's inputs actually spend (in-DB), `dests` are the tx's own outputs. Picking one of
    each keeps a manual link WITHIN the transaction (Invariant #5 — never a cross-tx edge); `add_manual_link`
    re-validates on write, so this only powers the UI pickers. Empty lists for a non-BTC / unknown tx."""
    def _candidate(r) -> dict:
        addr = r["addr"] or "?"
        return {"id": r["id"], "output_index": r["output_index"], "amount": r["amount"], "address": addr,
                "label": f"out #{r['output_index']} · {r['amount']} sat · {addr}"}

    sources = conn.execute(
        "SELECT o.id, o.output_index, o.amount, a.address_display AS addr "
        "FROM tx_input i JOIN tx_output o ON o.id=i.prev_output_id "
        "LEFT JOIN address a ON a.id=o.address_id "
        "WHERE i.transaction_id=? AND i.prev_output_id IS NOT NULL ORDER BY i.input_index", (tx_id,)).fetchall()
    dests = conn.execute(
        "SELECT o.id, o.output_index, o.amount, a.address_display AS addr "
        "FROM tx_output o LEFT JOIN address a ON a.id=o.address_id "
        "WHERE o.transaction_id=? ORDER BY o.output_index", (tx_id,)).fetchall()
    return {"sources": [_candidate(r) for r in sources], "dests": [_candidate(r) for r in dests]}


@app.get("/api/trace/{trace_id}/next_hops")
def api_trace_next_hops(trace_id: str, conn=Depends(get_case_conn)) -> dict:
    """FN-16 (guided expansion): PROPOSE candidate next hops from the trace's frontier — outgoing facts
    ALREADY in the case that leave a terminal node. Strictly read-only: the investigator picks which to add
    (EVM via /transfer, BTC via a within-tx /link); the tool never auto-adds or attributes flow."""
    from .services.tracing import trace_next_hops

    _require_trace(conn, trace_id)
    return trace_next_hops(conn, trace_id)


@app.get("/api/trace/{trace_id}/bridge_links")
def api_trace_bridge_links(trace_id: str, conn=Depends(get_case_conn)) -> dict:
    """FN-17: the trace's cross-chain bridge links (labeled investigator claims, with each side's chain)."""
    from .services.tracing import trace_bridge_links

    _require_trace(conn, trace_id)
    return {"bridge_links": trace_bridge_links(conn, trace_id)}


@app.post("/api/trace/{trace_id}/bridge")
def api_trace_add_bridge(trace_id: str, body: BridgeLinkBody, conn=Depends(get_case_conn)) -> dict:
    """FN-17: assert a manual CROSS-CHAIN bridge link — value leaving via a movement on chain A corresponds
    to value arriving via a movement on chain B. A `basis='investigator'` CLAIM, never a synthesized fact
    (Invariant #5); both movements must exist and cross chains. Manual only (no automated detection, RJ-02)."""
    from .services.tracing import add_bridge_link

    _require_trace(conn, trace_id)
    try:
        link_id = add_bridge_link(conn, trace_id=trace_id,
            src_subject_type=body.src_subject_type, src_subject_id=body.src_subject_id,
            dst_subject_type=body.dst_subject_type, dst_subject_id=body.dst_subject_id,
            confidence=body.confidence, note=(body.note or "").strip() or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "bridge_link_id": link_id}


@app.post("/api/trace/{trace_id}/transfer/{trace_transfer_id}/retract")
def api_retract_trace_transfer(trace_id: str, trace_transfer_id: str, body: RetractBody,
                               conn=Depends(get_case_conn)) -> dict:
    """FN-04: retract an EVM trace edge (append-only — the edge row + retraction persist; the edge drops
    out of the effective trace, graph, and report). Idempotent. Re-adding the edge afterwards works."""
    from .services.tracing import retract_trace_transfer

    _require_trace(conn, trace_id)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="a retraction needs a non-empty reason")
    try:
        retraction_id = retract_trace_transfer(conn, trace_transfer_id=trace_transfer_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "retraction_id": retraction_id}


@app.post("/api/trace/{trace_id}/link/{trace_btc_link_id}/retract")
def api_retract_trace_btc_link(trace_id: str, trace_btc_link_id: str, body: RetractBody,
                               conn=Depends(get_case_conn)) -> dict:
    """FN-04: retract a Bitcoin trace link (mirrors the EVM edge retraction — append-only, idempotent)."""
    from .services.tracing import retract_trace_btc_link

    _require_trace(conn, trace_id)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="a retraction needs a non-empty reason")
    try:
        retraction_id = retract_trace_btc_link(conn, trace_btc_link_id=trace_btc_link_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "retraction_id": retraction_id}


@app.post("/api/trace/{trace_id}/retract")
def api_retract_trace(trace_id: str, body: RetractBody, conn=Depends(get_case_conn)) -> dict:
    """v1.3.1: retract (soft-delete) a WHOLE trace — append-only. The trace + its edges persist in-DB, but the
    trace drops out of the trace list / graph overlay / report / activity. A reason is required (mirrors the
    edge/link retraction); idempotent. 404 for an unknown trace."""
    from .services.tracing import retract_trace

    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="a retraction needs a non-empty reason")
    try:
        retraction_id = retract_trace(conn, trace_id=trace_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "retraction_id": retraction_id}


@app.post("/api/trace/{trace_id}/label")
def api_set_trace_label(trace_id: str, body: LabelBody, conn=Depends(get_case_conn)) -> dict:
    """Name/relabel a trace/path (feature 5). Persisted as an investigator label; the report + graph use
    it as the trace's display name. Returns the rebuilt graph so trace-edge names update immediately."""
    from .services.investigator import set_label

    if conn.execute("SELECT 1 FROM trace WHERE id=?", (trace_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="trace not found")
    try:
        set_label(conn, target_type="trace", target_id=trace_id, label=body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}  # EFF-01: the client refetches /api/view; never build a full graph it discards


class ExpandRequest(BaseModel):
    chain: str
    address: str
    bounds: dict | None = None  # validated per-connector; tighten to a TypedDict in a later phase


@app.post("/api/graph/expand")
def api_expand(req: ExpandRequest, conn=Depends(get_case_conn),
               orch: Orchestrator = Depends(get_orchestrator)) -> dict:
    """Ingest/expand an address: pull its transactions via the orchestrator (honoring bounds), return the
    updated graph and whether any of this expansion's source_queries were truncated (``partial``). This is
    also how a BRAND-NEW empty case is seeded (the add-address control). Failures return a clean ``error``
    (never a 500), and the most common cause — an EVM chain with no Etherscan key — gets actionable
    guidance instead of a raw upstream message. Offline mode short-circuits BEFORE any dispatch."""
    from .connectors.etherscan import CHAIN_TO_CHAINID
    from .secrets import get_secret
    from .services import jobs
    from .services.refetch_diff import capture_snapshot, compute_diff
    from .services.settings_store import is_offline

    # Offline-first (P5): block ingest up-front, connector-independent, so the message is always clear
    # (and no chain/key edge case can mask it). Cached data, views, reports, export still work.
    if is_offline():
        return {"error": "Offline mode is on — turn it off in Settings to fetch new data "
                         "(cached data, views, reports and export still work).",
                "offline": True, "graph": build_graph(conn), "partial": False}

    # P8.7.2 — the fetch is an observable, cancelable JOB: the connector base bumps "pages fetched" +
    # "rate-limited" on this job and honors a cancel between pages. We return the FACTS as soon as they
    # persist (fast path restored) — valuation is DECOUPLED (the client auto-kicks /api/valuation/run).
    job = jobs.start("ingest")
    job.phase = "fetching"
    existing = {r[0] for r in conn.execute("SELECT id FROM source_query").fetchall()}
    # P23/FN-13: snapshot the fact-state BEFORE the re-fetch so we can report what it changed (read-only).
    before_snap = capture_snapshot(conn)
    try:
        orch.get_transactions(conn, req.chain, req.address, req.bounds or {})
    except jobs.JobCancelled:
        # A cancel is checked only at page boundaries, BEFORE a write — so completed source_queries are
        # intact and a canceled ingest leaves a CONSISTENT case (no partial/orphaned rows).
        job.mark_canceled()
        return {"canceled": True, "graph": build_graph(conn), "partial": True}
    except (NoConnectorError, ConnectorError, ValueError) as exc:
        job.fail(str(exc))
        msg = str(exc)
        needs_key = None
        # EVM chain + no Etherscan key + no connector could serve it -> point the user at Settings.
        if req.chain.lower() in CHAIN_TO_CHAINID and not get_secret("etherscan"):
            msg = ("EVM ingest needs a free Etherscan API key — add one in Settings -> Connectors "
                   "(Etherscan), then try again. Bitcoin addresses need no key.")
            needs_key = "etherscan"
        out = {"error": msg, "graph": build_graph(conn), "partial": False}
        if needs_key:
            out["needs_key"] = needs_key
        return out
    partial = any(status == "partial"
                  for (sid, status) in conn.execute("SELECT id, status FROM source_query").fetchall()
                  if sid not in existing)
    job.finish({"partial": partial})
    out = {"graph": build_graph(conn), "partial": partial}
    # P23/FN-13: what this re-fetch changed — "+N transfers, K provisional→final, C corrected" (Inv #6/#7).
    # Best-effort + read-only: a diff failure must never break the re-fetch itself.
    try:
        out["diff"] = compute_diff(conn, before_snap)
    except Exception:  # noqa: BLE001 — surfacing the diff is non-critical; the facts are already persisted
        pass
    return out


# How many movements one valuation pass attempts — bounds a runaway pass on a huge case (the circuit-
# breaker also stops a dead price source after a few consecutive failures). P8.7.2.
VALUATION_PASS_CAP = 3000


def _start_valuation_job(case_path: str):
    """Run a valuation pass on the case in a BACKGROUND thread (P8.7.2) so it never blocks a request.
    Reports progress + honors cancel via the jobs registry; offline-guarded by the caller. The thread
    opens its OWN connection (the request's is closed when the handler returns)."""
    import threading

    from .connectors.defillama import DeFiLlamaConnector
    from .db.connection import get_connection
    from .services import jobs
    from .services.valuation import value_movements

    job = jobs.start("valuation")

    def _run() -> None:
        jobs.bind(job)  # bind THIS thread's worker job so the connector hooks report to it (not the global)
        try:
            conn = get_connection(case_path, create_parents=False)
            connector = DeFiLlamaConnector(settings=get_settings())
            # FN-05 — the shared price cache is a PURE optimization; opening it must NEVER block valuation.
            cache_conn = None
            try:
                from .app_paths import user_data_dir
                from .db.shared_cache import get_cache_connection, migrate_cache
                cache_path = user_data_dir() / "library_cache.db"
                migrate_cache(cache_path)
                cache_conn = get_cache_connection(cache_path)
            except Exception:
                cache_conn = None
            try:
                res = value_movements(conn, connector, limit=VALUATION_PASS_CAP, job=job,
                                      cache_conn=cache_conn)
                job.finish(res)
            finally:
                connector.close()
                if cache_conn is not None:
                    cache_conn.close()
                conn.close()
        except jobs.JobCancelled:
            job.mark_canceled()
        except Exception as exc:  # connector/transport hiccup — surface on the job, never a server crash
            job.fail(str(exc))

    threading.Thread(target=_run, name="bih-valuation", daemon=True).start()
    return job


@app.post("/api/valuation/run")
def api_valuation_run(conn=Depends(get_case_conn),
                      path: str | None = Depends(get_case_db_path)) -> dict:
    """Start a BACKGROUND valuation pass over the active case (P8.7.2 — non-blocking; poll /api/jobs/active
    for progress, cancel via /api/jobs/cancel). Offline-aware: a 409 when offline. Reuses value_movements
    (429 circuit-breaker + batching + the pass cap); a missing price writes no row (honest gap)."""
    from .services.settings_store import is_offline

    if is_offline():
        raise HTTPException(status_code=409,
                            detail="offline mode is on — turn it off to value movements from DeFiLlama "
                                   "(cached data, views, reports and export still work).")
    if not path:
        raise HTTPException(status_code=503, detail="no active case")
    job = _start_valuation_job(path)
    return {"ok": True, "started": True, "job_id": job.id}


# --- long-operation jobs (progress + cancel) -----------------------------------------------

@app.get("/api/jobs/active")
def api_job_active() -> dict:
    """The active long operation's live progress (ingest fetch / valuation), or ``null``. Polled by the
    Add-address modal + the Value action to show a real progress line + offer Cancel (P8.7.2)."""
    from .services import jobs

    j = jobs.active()
    return {"job": j.status() if j is not None else None}


@app.post("/api/jobs/cancel")
def api_job_cancel() -> dict:
    """Cancel the active long operation (cooperative; the worker stops at the next page boundary, leaving
    a consistent case). Returns whether a running job was canceled."""
    from .services import jobs

    return {"ok": True, "canceled": jobs.cancel_active()}


# --- chains (the add-address control) ------------------------------------------------------

@app.get("/api/chains")
def api_chains() -> dict:
    """The chains the app can INGEST, for the add-address control. The EVM list is sourced from the
    Etherscan V2 connector's own map (so the UI never offers a chain that fails at ingest); Bitcoin is
    keyless via Esplora."""
    return {"evm": _evm_chains(), "btc": ["bitcoin"]}


# --- report generation (from the UI) -------------------------------------------------------

class ReportViewParams(BaseModel):
    """The active /api/view state the Report button passes so the report renders the CURRENT curated view
    (P8.7.1 #2) — mirrors the /api/view query params; coerced identically by ``_report_view_params``."""
    focus: str | None = None
    hops: int = 1
    node_cap: int = 150
    group_dust: bool = True
    dust_floor_usd: float = 1.0
    dust_floor_native: float = 0.001
    value_floor_usd: float = 0.0
    edge_kinds: str | None = None
    only_flagged: bool = False
    user_dust_usd: float | None = None
    expand: str | None = None
    value_basis: str = "usd"
    group_denominations: bool = False
    show_unverified: bool = False
    fold_poison: bool = True
    denom_filters: str | None = None
    community: bool = False


class ReportBody(BaseModel):
    title: str | None = None
    view: ReportViewParams | None = None   # present -> render the bounded current view; absent -> full case


def _report_view_params(v: "ReportViewParams") -> dict:
    """Coerce a ReportViewParams into build_view kwargs, identically to /api/view (so the report matches
    exactly what the canvas showed)."""
    import json as _json

    df: dict = {}
    if v.denom_filters:
        try:
            parsed = _json.loads(v.denom_filters)
            if isinstance(parsed, dict):
                df = {str(k): {kk: float(vv) for kk, vv in (val or {}).items() if kk in ("min", "fold")}
                      for k, val in parsed.items() if isinstance(val, dict)}
        except (ValueError, TypeError):
            df = {}
    return dict(
        focus=v.focus, hops=max(1, v.hops), node_cap=max(1, v.node_cap), group_dust=v.group_dust,
        dust_floor_usd=max(0.0, v.dust_floor_usd), dust_floor_native=max(0.0, v.dust_floor_native),
        value_floor_usd=max(0.0, v.value_floor_usd), edge_kinds=_csv(v.edge_kinds) or None,
        only_flagged=v.only_flagged,
        user_dust_usd=(v.user_dust_usd if v.user_dust_usd and v.user_dust_usd > 0 else None),
        expand=tuple(_csv(v.expand)),
        value_basis=("native" if v.value_basis == "native" else "usd"),
        group_denominations=v.group_denominations, show_unverified=v.show_unverified,
        fold_poison=v.fold_poison, denom_filters=df, community_detect=v.community)


class OpenFileBody(BaseModel):
    path: str


def _os_open(path: Path) -> None:
    """Open a file with the OS default opener (windowed app). Caller MUST constrain ``path`` first."""
    import subprocess

    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - path is constrained to the active case dir by the caller
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.post("/api/report")
def api_report(body: ReportBody, conn=Depends(get_case_conn)) -> dict:
    """Generate an immutable report of the ACTIVE case (full-case scope — what /api/graph renders) and
    return its ``content_hash`` (the immutability proof) plus where the HTML/PDF landed. A missing OS
    browser engine is NOT an error: the HTML (the hashed source of truth) is always written and
    ``pdf_skip_reason`` explains the skipped PDF. Findings + annotations already flow into the report."""
    from .services import cases
    from .services.reporting import generate_report

    path = cases.active_case_path()
    if not path:
        raise HTTPException(status_code=503, detail="no active case")
    case_dir = Path(path).parent
    title = (body.title or "").strip()
    if not title:
        meta = cases.active_meta()
        title = (meta or {}).get("title") or "Investigation Report"
    view_params = _report_view_params(body.view) if body.view is not None else None
    try:
        res = generate_report(conn, case_dir=case_dir, title=title, view_params=view_params)
    except Exception as exc:  # graph/render/IO — surface clearly, don't 500-with-traceback
        raise HTTPException(status_code=500, detail=f"report generation failed: {exc}")
    return {
        "ok": True,
        "report_id": res["report_id"],
        "content_hash": res["content_hash"],
        "html_path": str(res["html_path"]),
        "pdf_path": str(res["pdf_path"]) if res["pdf_path"] else None,
        "engine": res["engine"],
        "pdf_skip_reason": res["pdf_skip_reason"],
    }


@app.post("/api/report/open")
def api_open_report(body: OpenFileBody) -> dict:
    """Open a generated report file with the OS default opener (windowed app). SECURITY: only opens a
    file that resolves UNDER the active case's directory (never an arbitrary path), and only if it
    exists — so this can't be used to open files elsewhere on disk."""
    from .services import cases

    active = cases.active_case_path()
    if not active:
        raise HTTPException(status_code=503, detail="no active case")
    case_dir = Path(active).parent.resolve()
    target = Path(body.path).resolve()
    if not target.is_relative_to(case_dir):
        raise HTTPException(status_code=400, detail="can only open files within the active case")
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found (the PDF may have been skipped)")
    try:
        _os_open(target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not open the file: {exc}")
    return {"ok": True, "opened": str(target)}


# --- intel enrichment (free attribution + sanctions pillars) -------------------------------

@app.post("/api/intel/check")
def api_intel_check(conn=Depends(get_case_conn)) -> dict:
    """Run the free intel pillars (OFAC SDN + GraphSense, + Chainalysis if a free key is set) against the
    ACTIVE case from the bundled snapshots (works OFFLINE). Writes sourced CLAIMS (Inv #3/#4) — the
    sanctioned halo + GraphSense entity then appear on matching addresses on the next graph render. It
    enriches with claims; it never writes a fact about the chain."""
    from .services.intel import check_intel

    try:
        return {"ok": True, **check_intel(conn)}
    except Exception as exc:  # a corrupt snapshot etc. — clear error, not a 500-with-traceback
        raise HTTPException(status_code=500, detail=f"intel check failed: {exc}")


@app.post("/api/intel/refresh")
def api_intel_refresh() -> dict:
    """Refresh the OFAC SDN snapshot from source (online) and use it as the override. Offline-aware: a
    409 when offline (the bundled snapshot keeps working with no network)."""
    from .services.intel import refresh_ofac

    try:
        return {"ok": True, "ofac": refresh_ofac()}
    except RuntimeError as exc:  # offline
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"could not refresh from source: {exc}")


# --- clustering (P8.8) — each heuristic is a SEPARATE, reversible, confidence-tagged producer ----------

class ClusteringBody(BaseModel):
    """Apply/preview a named clustering heuristic with its parameters (e.g. require_agree, a_max_eth)."""
    name: str
    params: dict | None = None


class ClusteringUndoBody(BaseModel):
    source_query_id: str


@app.get("/api/clustering/heuristics")
def api_clustering_heuristics() -> dict:
    """The catalog the Clustering panel renders: co-spend (always on) + each opt-in heuristic + Leiden
    community (visual-only). Every new heuristic defaults OFF (conservative)."""
    from .services.clustering import service as clustering
    return {"heuristics": clustering.list_heuristics()}


@app.get("/api/clustering/summary")
def api_clustering_summary(conn=Depends(get_case_conn)) -> dict:
    """Per-heuristic clusters formed (size + confidence), side-by-side (Inv #4) — drives the panel/report."""
    from .services.clustering import service as clustering
    return {"summary": clustering.cluster_summary(conn), "runs": clustering.list_runs(conn)}


@app.post("/api/clustering/preview")
def api_clustering_preview(body: ClusteringBody, conn=Depends(get_case_conn)) -> dict:
    """Compute what a heuristic WOULD merge, WITHOUT persisting — so the investigator previews before apply."""
    from .services.clustering import service as clustering
    try:
        return {"ok": True, "preview": clustering.preview(conn, body.name, body.params)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/clustering/apply")
def api_clustering_apply(body: ClusteringBody, conn=Depends(get_case_conn)) -> dict:
    """Apply a heuristic as a RUN (one source_query). Writes confidence-tagged, provenance-carrying,
    side-by-side cluster memberships (Inv #3/#4) — reversible via /undo or a per-address split."""
    from .services.clustering import service as clustering
    try:
        return {"ok": True, **clustering.apply(conn, body.name, body.params)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/clustering/undo")
def api_clustering_undo(body: ClusteringUndoBody, conn=Depends(get_case_conn)) -> dict:
    """Undo a clustering RUN as a unit: retract every still-active membership it wrote (append-only)."""
    from .services.clustering import service as clustering
    return {"ok": True, **clustering.undo_run(conn, body.source_query_id)}


class ApprovalsFetchBody(BaseModel):
    address: str
    chain: str = "ethereum"
    bounds: dict | None = None


@app.post("/api/approvals/fetch")
def api_fetch_approvals(body: ApprovalsFetchBody, conn=Depends(get_case_conn)) -> dict:
    """LOG-06: fetch the ERC-20 Approval events where ``address`` is the owner (Etherscan getLogs) and write
    ``erc20_approval`` rows — the data the EVM self-authorization heuristic needs. Then run the heuristic via
    /api/clustering/apply (name='evm-self-authorization'). Clean errors (never a raw 500); offline/no-key
    guarded like ingest."""
    from .connectors.etherscan import CHAIN_TO_CHAINID, EtherscanConnector
    from .secrets import get_secret
    from .services.settings_store import is_offline

    if body.chain.lower() not in CHAIN_TO_CHAINID:
        raise HTTPException(status_code=400, detail=f"{body.chain!r} is not an EVM chain with approval logs")
    if is_offline():
        return {"error": "Offline mode is on — turn it off to fetch approvals.", "offline": True}
    key = get_secret("etherscan")
    if not key:
        return {"error": "Fetching approvals needs a free Etherscan API key — add one in Settings → "
                         "Connectors (Etherscan).", "needs_key": "etherscan"}
    connector = EtherscanConnector(api_key=key, settings=get_settings())
    try:
        res = connector.get_erc20_approvals(conn, body.chain, body.address, body.bounds or {})
    except (ConnectorError, ValueError) as exc:
        return {"error": str(exc)}
    finally:
        connector.close()
    return {"ok": True, **res}


# --- frontend (one-click packaging) --------------------------------------------------------
# Serve the built SPA from this same origin when it exists (no-op in dev / CI without a build).
# Defined last so the explicit /api and /health routes above always take precedence.
from .web import mount_frontend  # noqa: E402

mount_frontend(app)
