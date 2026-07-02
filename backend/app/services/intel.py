"""Intel enrichment (P8.7 #4) — run the free attribution/risk import pillars against a case.

A live-ingested address shows on-chain FACTS only; the free intel pillars (OFAC SDN sanctions +
GraphSense attribution) are structured imports with no UI trigger, so the sanctioned halo / entity ring
never appears. ``check_intel`` runs those importers against the active case so their PROVENANCE-CARRYING
sourced CLAIMS land (Invariants #3/#4) — side-by-side, never merged, never asserted as facts about the
chain. Sources, in order:

  * OFAC SDN (``imports/ofac.py``) + GraphSense TagPacks (``imports/graphsense.py``) read from BUNDLED
    snapshots (``backend/app/intel/*`` via ``resource_path`` — works fully OFFLINE) or a configured
    override / a "refresh from source" download. Each snapshot is stamped with a date.
  * Chainalysis sanctions (``connectors/chainalysis.py``) runs only when a free key is set AND we're
    online — it screens each case address via the public API.

Running intel READS/ENRICHES: it WRITES sourced claims (risk_assessment / attribution / entity), NEVER a
fact about the chain. P8.7.1 #1 — intel is strictly ADDRESS-SCOPED: a claim is written ONLY for an address
ALREADY in the case (``only_known_addresses=True``). A snapshot's other addresses are never injected, so
running intel on a case whose addresses are not in the OFAC/GraphSense snapshot yields ZERO claims, ZERO
injected addresses, and ZERO phantom entity in the DB or the report.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..app_paths import BUNDLED_RESOURCES, resource_path
from . import settings_store

_OFAC_BUNDLED = BUNDLED_RESOURCES["intel_ofac_sdn"]
_GS_BUNDLED = BUNDLED_RESOURCES["intel_graphsense_tagpack"]


# --------------------------------------------------------------------------- snapshot resolution + dates

def _resolve(name: str, bundled_rel: str) -> tuple[Path, bool]:
    """(path, is_override). The configured override if it exists, else the bundled snapshot."""
    override = settings_store.intel_source(name)
    if override and Path(override).exists():
        return Path(override), True
    return resource_path(bundled_rel), False


def ofac_path() -> Path:
    return _resolve("ofac", _OFAC_BUNDLED)[0]


def graphsense_path() -> Path:
    return _resolve("graphsense", _GS_BUNDLED)[0]


def _ofac_date(path: Path) -> str | None:
    try:
        m = re.search(r"<Publish_Date>\s*([^<]+?)\s*</Publish_Date>", path.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def _gs_date(path: Path) -> str | None:
    try:
        m = re.search(r"(?m)^lastmod:\s*(.+?)\s*$", path.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def snapshot_info() -> dict:
    """The intel-source snapshots' paths + dates + whether an override is in effect (for Settings)."""
    op, o_over = _resolve("ofac", _OFAC_BUNDLED)
    gp, g_over = _resolve("graphsense", _GS_BUNDLED)
    return {
        "ofac": {"path": str(op), "date": _ofac_date(op), "override": o_over, "exists": op.exists()},
        "graphsense": {"path": str(gp), "date": _gs_date(gp), "override": g_over, "exists": gp.exists()},
    }


# --------------------------------------------------------------------------- run intel against a case

def check_intel(conn, *, now: str | None = None, run_chainalysis: bool = True) -> dict:
    """Run the free intel pillars against the active case. Writes sourced claims (Inv #3/#4); returns a
    per-source summary. Works OFFLINE with the bundled snapshots; Chainalysis runs only when keyed+online."""
    from ..connectors.imports.graphsense import GraphSenseImporter
    from ..connectors.imports.ofac import OfacSdnImporter

    out: dict = {"sources": [], "wrote_claims": True}

    # P8.7.1 #1 — intel ENRICHES, it must never INJECT: only addresses ALREADY in the case get claims, so
    # a snapshot's other addresses (the bundled OFAC/GraphSense entries) never pollute an unrelated case.
    op = ofac_path()
    if op.exists():
        ofac = OfacSdnImporter()
        risk = ofac.get_risk(conn, str(op), now=now, only_known_addresses=True)
        attr = ofac.get_attributions(conn, str(op), now=now, only_known_addresses=True)
        out["ofac"] = {"sanctioned": risk.get("risks", 0), "attributions": attr.get("attributions", 0),
                       "snapshot_date": _ofac_date(op)}
        out["sources"].append("ofac-sdn")

    gp = graphsense_path()
    if gp.exists():
        gs = GraphSenseImporter()
        gattr = gs.get_attributions(conn, str(gp), now=now, only_known_addresses=True)
        gent = gs.get_entities(conn, str(gp), now=now, only_known_addresses=True)
        out["graphsense"] = {"attributions": gattr.get("attributions", 0),
                             "memberships": gent.get("memberships", gent.get("entities_created", 0)),
                             "snapshot_date": _gs_date(gp)}
        out["sources"].append("graphsense")

    if run_chainalysis:
        ca = _maybe_chainalysis(conn, now=now)
        if ca is not None:
            out["chainalysis"] = ca
            if "error" not in ca:
                out["sources"].append("chainalysis")

    return out


def _maybe_chainalysis(conn, *, now: str | None) -> dict | None:
    """Screen each case address via Chainalysis IF a free key is set and we're online — else ``None``
    (skipped) so the offline bundled-snapshot path always works."""
    from ..secrets import get_secret
    from .settings_store import is_offline

    if not get_secret("chainalysis_api_key") or is_offline():
        return None
    from ..connectors.chainalysis import ChainalysisSanctionsConnector

    rows = conn.execute("SELECT chain, address_display FROM address").fetchall()
    conn_ca = ChainalysisSanctionsConnector()
    checked = 0
    try:
        for r in rows:
            try:
                conn_ca.get_risk(conn, r["chain"], r["address_display"], now=now)
                checked += 1
            except Exception:  # one address failing must not abort the whole sweep
                continue
    finally:
        conn_ca.close()
    return {"checked": checked}


# --------------------------------------------------------------------------- refresh from source (online)

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"


def refresh_ofac(*, dest_dir: Path | None = None) -> dict:
    """Download the current OFAC SDN XML to user-data + set it as the override. Offline-aware (raises a
    clear error when offline). The bundled snapshot keeps working if this is never run."""
    from .settings_store import is_offline

    if is_offline():
        raise RuntimeError("offline mode is on — turn it off to refresh intel from source")
    from ..app_paths import user_data_dir
    from ..connectors.base import fetch_bytes  # SEC-13: httpx stays isolated to the connectors module

    dest_dir = dest_dir or (user_data_dir() / "intel")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "sdn.xml"
    dest.write_bytes(fetch_bytes(OFAC_SDN_URL, timeout=60.0))
    settings_store.set_intel_source("ofac", dest)
    return {"ok": True, "path": str(dest), "date": _ofac_date(dest), "bytes": dest.stat().st_size}
