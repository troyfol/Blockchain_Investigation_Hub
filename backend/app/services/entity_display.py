"""Entity display policy (Phase 6 step 5).

Resolution chases ``merged_into`` to the canonical entity. Display is:
- **canonical** when ``entity.canonical_membership_id`` is set (the investigator curated it);
- else **contested** when active memberships come from more than one source (shown side-by-side,
  never collapsed — Invariant #4);
- else **resolved** (a single, unambiguous grouping).

Active memberships = memberships whose entity resolves to the canonical entity and that have NOT
been retracted (append-only retraction). All active memberships are always returned side-by-side.
"""

from __future__ import annotations

from .entities import resolve

# P8.8.1 — descriptive (NOT identity-asserting) display labels for auto-clusters that carry no real name,
# so the report/panel read "Co-spend cluster (166 addresses)" instead of "(unnamed cospend-cluster entity)".
# Keyed by entity_type first (the specific heuristic), then origin.
_CLUSTER_LABEL_BY_TYPE = {
    "btc-change": "Change-heuristic cluster",
    "evm-deposit-reuse": "Deposit-reuse cluster",
    "evm-airdrop": "Airdrop cluster",
    "evm-self-auth": "Self-authorization cluster",
    "same-address": "Same-address group",
}
_CLUSTER_LABEL_BY_ORIGIN = {
    "cospend-cluster": "Co-spend cluster",
    "heuristic-cluster": "Heuristic cluster",
    "investigator": "Investigator group",
}


def cluster_display_name(origin: str | None, entity_type: str | None, count: int) -> str:
    """A descriptive label for an unnamed auto-cluster — a DESCRIPTION (kind + size), not an identity claim.
    The stable entity id is shown alongside it by the caller."""
    base = (_CLUSTER_LABEL_BY_TYPE.get(entity_type or "")
            or _CLUSTER_LABEL_BY_ORIGIN.get(origin or "") or "Cluster")
    return f"{base} ({count} address{'es' if count != 1 else ''})"


def active_memberships(conn, canonical_id: str) -> list[dict]:
    out = []
    for m in conn.execute("SELECT * FROM entity_membership").fetchall():
        if resolve(conn, m["entity_id"]) != canonical_id:
            continue
        retracted = conn.execute(
            "SELECT 1 FROM entity_membership_retraction WHERE membership_id=?", (m["id"],)).fetchone()
        if retracted:
            continue
        row = {k: m[k] for k in m.keys()}
        # P8.7.1 #5 — surface the MEMBER ADDRESS so identical-provenance rows (one source covering several
        # addresses) read as distinct facts in the report, not an erroneous duplicate.
        a = conn.execute("SELECT address, address_display FROM address WHERE id=?",
                         (m["address_id"],)).fetchone()
        row["address"] = (a["address_display"] or a["address"]) if a else None
        out.append(row)
    return out


def _display_name(conn, e, memberships: list[dict]) -> str | None:
    """The entity's human display name (P8.7.1 #5): a real curated/ActorPack ``entity.name`` wins; a raw
    slug (``name == external_id``, e.g. 'hydramarket' from a TagPack with no ActorPack) falls back to a
    member address's attribution label (e.g. 'Hydra Market'). Deterministic; never merges contested labels."""
    name = e["name"]
    if name and name != e["external_id"]:
        return name  # a genuine name, not the slug
    addr_ids = [m["address_id"] for m in memberships if m.get("address_id")]
    if addr_ids:
        ph = ",".join("?" * len(addr_ids))
        row = conn.execute(
            f"SELECT label FROM attribution WHERE address_id IN ({ph}) AND label IS NOT NULL "
            f"ORDER BY source, label LIMIT 1", addr_ids).fetchone()
        if row and row["label"]:
            return row["label"]
    if not name:  # an unnamed auto-cluster -> a descriptive (kind + size) label, never "(unnamed … entity)"
        return cluster_display_name(e["origin"], e["entity_type"], len(memberships))
    return name  # no better label -> keep the slug (external_id stays exposed for honesty)


def entity_display(conn, entity_id: str) -> dict:
    canonical_id = resolve(conn, entity_id)
    e = conn.execute(
        "SELECT name, external_id, entity_type, origin, canonical_membership_id FROM entity WHERE id=?",
        (canonical_id,)).fetchone()
    memberships = active_memberships(conn, canonical_id)
    sources = {m["source"] for m in memberships}

    if e["canonical_membership_id"]:
        status = "canonical"
    elif len(sources) > 1:
        status = "contested"  # multiple sources weigh in — never auto-collapse
    else:
        status = "resolved"

    return {
        "entity_id": canonical_id,
        "name": _display_name(conn, e, memberships),  # display name (slug -> attribution-label fallback)
        "external_id": e["external_id"],              # the raw id, kept visible for honesty
        "entity_type": e["entity_type"],
        "origin": e["origin"],
        "status": status,
        "canonical_membership_id": e["canonical_membership_id"],
        "memberships": memberships,  # all active claims, side-by-side (now carrying the member address)
    }
