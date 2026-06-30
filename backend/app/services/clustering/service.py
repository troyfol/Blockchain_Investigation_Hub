"""Clustering orchestration: list / preview / apply / undo, plus a per-heuristic cluster summary.

Conservative defaults: co-spend stays ON (it runs at ingest); EVERY heuristic here defaults OFF and is
applied explicitly. Each apply is a RUN (one ``source_query``); undo retracts that run's memberships as a
unit (append-only retraction — no row is ever rewritten, Invariant; round-trips with apply). All clusters
are SIDE-BY-SIDE claims (Invariant #4): an address may belong to a co-spend entity AND a change-heuristic
entity AND a deposit-reuse entity at once; the panel/report show each membership's heuristic + confidence.
"""

from __future__ import annotations

from ...db import repository as repo
from ...db.repository import utc_now_iso
from ...models import EntityMembershipRetraction, SourceQuery
from ...provenance.atomic import write_with_provenance
from ..entities import resolve
from . import btc_change, evm

# The registry of explicitly-applicable heuristics. co-spend is NOT here (it is the always-on ingest
# clustering); these all default OFF. Each entry: chain, the apply fn, the preview fn, and a label.
HEURISTICS: dict[str, dict] = {
    "btc-change": {"chain": "bitcoin", "label": "BTC change-address (BlockSci 0.7)",
                   "apply": btc_change.cluster_btc_change, "preview": btc_change.preview_change_clusters,
                   "default_off": True},
    "evm-deposit-reuse": {"chain": "evm", "label": "EVM deposit-address reuse (Victor 2020)",
                          "apply": evm.cluster_deposit_reuse, "preview": evm.preview_deposit_reuse,
                          "default_off": True},
    "evm-airdrop": {"chain": "evm", "label": "EVM airdrop multi-participation (Victor 2020)",
                    "apply": evm.cluster_airdrop, "preview": evm.preview_airdrop, "default_off": True},
    "evm-self-authorization": {"chain": "evm", "label": "EVM self-authorization (Victor 2020)",
                               "apply": evm.cluster_self_authorization,
                               "preview": evm.preview_self_authorization, "default_off": True},
}


def list_heuristics() -> list[dict]:
    """The catalog the Clustering panel renders (co-spend listed first as the always-on baseline)."""
    out = [{"name": "cospend", "chain": "bitcoin", "label": "Co-spend (Meiklejohn 2013) — always on",
            "default_off": False, "always_on": True}]
    for name, h in HEURISTICS.items():
        out.append({"name": name, "chain": h["chain"], "label": h["label"],
                    "default_off": h["default_off"], "always_on": False})
    out.append({"name": "community", "chain": "any",
                "label": "Leiden community (Traag 2019) — VISUAL structure, not ownership; never persisted",
                "default_off": True, "always_on": False, "visual_only": True})
    return out


def preview(conn, name: str, params: dict | None = None) -> dict:
    if name not in HEURISTICS:
        raise ValueError(f"unknown heuristic {name!r}")
    return HEURISTICS[name]["preview"](conn, **(params or {}))


def apply(conn, name: str, params: dict | None = None, *, now: str | None = None) -> dict:
    if name not in HEURISTICS:
        raise ValueError(f"unknown heuristic {name!r}")
    res = HEURISTICS[name]["apply"](conn, **(params or {}), now=now or utc_now_iso())
    res["heuristic"] = name
    return res


# --------------------------------------------------------------------------- undo a run as a unit

def undo_run(conn, source_query_id: str, *, reason: str = "heuristic-undo", now: str | None = None) -> dict:
    """Reverse a clustering run AS A UNIT: retract every still-active membership written by that run's
    ``source_query`` (append-only). The entities it created remain but, with no active memberships, drop
    out of every display (entity_display only surfaces active memberships) — a clean, auditable undo.

    Scope note: this retracts only THIS run's own memberships. If the investigator manually SPLIT an address
    out of the run earlier (``split_address`` — a deliberate investigator action that minted its own
    investigator entity), that manual decision is independent and is NOT reversed by undoing the auto run."""
    now = now or utc_now_iso()
    rows = conn.execute(
        "SELECT m.id FROM entity_membership m WHERE m.source_query_id=? AND NOT EXISTS "
        "(SELECT 1 FROM entity_membership_retraction r WHERE r.membership_id=m.id)",
        (source_query_id,)).fetchall()
    if not rows:
        return {"retracted": 0, "source_query_id": source_query_id}
    sq = SourceQuery(connector="clustering-undo", capability="undo_run", endpoint="local",
                     params={"undone_source_query_id": source_query_id, "reason": reason},
                     requested_at=now, completed_at=now, status="ok")

    def write(c, sqid):
        n = 0
        for r in rows:
            repo.insert_entity_membership_retraction(c, EntityMembershipRetraction(
                membership_id=r["id"], reason=reason, source="clustering-undo", method="undo-run"),
                sqid, now=now)
            n += 1
        return {"retracted": n}

    _, res = write_with_provenance(conn, sq, write)
    res["source_query_id"] = source_query_id
    return res


def list_runs(conn) -> list[dict]:
    """Each clustering run (one source_query) with its heuristic + how many of its memberships are still
    active vs retracted — so the panel can offer per-run undo and show what's live."""
    runs = []
    for sq in conn.execute(
        "SELECT id, connector, capability, params, requested_at FROM source_query "
        "WHERE connector IN ('btc-change-clustering','evm-deposit-reuse','evm-airdrop-multi',"
        "'evm-self-authorization','cospend-clustering') ORDER BY requested_at, id").fetchall():
        total = conn.execute("SELECT COUNT(*) FROM entity_membership WHERE source_query_id=?",
                             (sq["id"],)).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM entity_membership m WHERE m.source_query_id=? AND NOT EXISTS "
            "(SELECT 1 FROM entity_membership_retraction r WHERE r.membership_id=m.id)",
            (sq["id"],)).fetchone()[0]
        runs.append({"source_query_id": sq["id"], "connector": sq["connector"],
                     "capability": sq["capability"], "params": sq["params"],
                     "requested_at": sq["requested_at"], "memberships": total, "active": active})
    return runs


# --------------------------------------------------------------------------- per-heuristic cluster summary

def cluster_summary(conn) -> dict:
    """For the panel/report: per heuristic (membership source), the clusters it formed with their sizes +
    confidences. Only ACTIVE memberships count. Clusters are keyed by the resolved entity id, so a split or
    an undo is reflected immediately. Side-by-side — never merged across heuristics (Invariant #4)."""
    by_source: dict[str, dict[str, list[dict]]] = {}
    for m in conn.execute(
        "SELECT m.id, m.entity_id, m.address_id, m.source, m.method, m.confidence FROM entity_membership m "
        "WHERE NOT EXISTS (SELECT 1 FROM entity_membership_retraction r WHERE r.membership_id=m.id)"
    ).fetchall():
        ent = resolve(conn, m["entity_id"])
        by_source.setdefault(m["source"], {}).setdefault(ent, []).append(
            {"address_id": m["address_id"], "method": m["method"], "confidence": m["confidence"]})

    out = {}
    for source, ents in by_source.items():
        clusters = []
        for ent, members in ents.items():
            if len(members) < 2:
                continue  # a singleton membership isn't a cluster
            confs = [x["confidence"] for x in members if x["confidence"] is not None]
            clusters.append({"entity_id": ent, "size": len(members),
                             "method": members[0]["method"],
                             "confidence_min": round(min(confs), 3) if confs else None,
                             "confidence_max": round(max(confs), 3) if confs else None,
                             "address_ids": sorted(x["address_id"] for x in members)})
        clusters.sort(key=lambda c: (-c["size"], c["entity_id"]))
        if clusters:
            out[source] = {"clusters": clusters, "n_clusters": len(clusters),
                           "n_addresses": sum(c["size"] for c in clusters)}
    return out
