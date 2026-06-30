"""Entity resolution (Phase 6; docs/algorithms.md §4/§5/§7).

Co-spend clustering (union-find over Bitcoin input addresses) at ingest, CoinJoin-flagged;
source-label / same-address / investigator memberships; first-class merge/split via
``entity.merged_into`` (memberships are NEVER rewritten — resolution chases the pointer) and
append-only ``entity_membership_retraction``.

Provenance: a clustering/heuristic run is itself recorded as a ``source_query`` (connector
``cospend-clustering`` / ``same-address-heuristic``) so the derived memberships carry provenance
(Invariant #3). Investigator memberships/retractions use ``source='investigator'`` (provenance
nullable). Co-spend memberships are inserted **once** per (entity, address) — deterministic and
append-only-safe (no rewrite). CONFIRM-AT-BUILD: CoinJoin thresholds/denominations (see PROGRESS).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import Entity, EntityMembership, EntityMembershipRetraction, SourceQuery
from ..provenance.atomic import write_with_provenance

# --- CoinJoin detection knobs (docs/algorithms.md §5; CONFIRM-AT-BUILD) -------------------
K_INPUTS = 5            # "many inputs"
K_EQUAL_OUTPUTS = 5     # ">= K equal-value outputs"
# Whirlpool pool sizes in satoshis (0.001 / 0.01 / 0.05 / 0.5 BTC).
WHIRLPOOL_DENOMS = (100_000, 1_000_000, 5_000_000, 50_000_000)

COSPEND_CONFIDENCE = 0.9
COINJOIN_CONFIDENCE = 0.5
SAME_ADDRESS_CONFIDENCE = 0.3


class UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)  # deterministic survivor (min id)

    def groups(self) -> list[set]:
        g: dict = defaultdict(set)
        for x in list(self.parent):
            g[self.find(x)].add(x)
        return list(g.values())


def resolve(conn, entity_id: str) -> str:
    """Follow ``merged_into`` to the terminal entity (the canonical id)."""
    seen = set()
    cur = entity_id
    while True:
        if cur in seen:
            raise ValueError(f"merged_into cycle at {cur!r}")
        seen.add(cur)
        row = conn.execute("SELECT merged_into FROM entity WHERE id=?", (cur,)).fetchone()
        if row is None or row["merged_into"] is None:
            return cur
        cur = row["merged_into"]


# --- CoinJoin --------------------------------------------------------------------------------

def is_probable_coinjoin(conn, tx_id: str) -> bool:
    out_vals = [int(r["amount"]) for r in
                conn.execute("SELECT amount FROM tx_output WHERE transaction_id=?", (tx_id,)).fetchall()]
    n_inputs = conn.execute(
        "SELECT COUNT(*) FROM tx_input WHERE transaction_id=?", (tx_id,)).fetchone()[0]
    if not out_vals:
        return False
    counts = Counter(out_vals)
    if n_inputs >= K_INPUTS and max(counts.values()) >= K_EQUAL_OUTPUTS:
        return True
    return any(counts.get(d, 0) >= K_EQUAL_OUTPUTS for d in WHIRLPOOL_DENOMS)


# --- co-spend clustering ---------------------------------------------------------------------

def _cospend_membership_entities(conn, address_id: str) -> set:
    """Resolved entity ids this address is a co-spend member of."""
    return {resolve(conn, r["entity_id"]) for r in conn.execute(
        "SELECT entity_id FROM entity_membership "
        "WHERE address_id=? AND source='cospend-heuristic' AND method='co-spend'",
        (address_id,)).fetchall()}


def cluster_cospend(conn, *, now: str | None = None) -> dict:
    """Union-find over Bitcoin input addresses; materialize co-spend clusters (>=2). Idempotent."""
    now = now or utc_now_iso()

    tx_inputs: dict = defaultdict(set)
    for r in conn.execute(
        "SELECT i.transaction_id, i.address_id FROM tx_input i "
        "JOIN transaction_ t ON t.id=i.transaction_id "
        "WHERE t.chain='bitcoin' AND i.address_id IS NOT NULL").fetchall():
        tx_inputs[r["transaction_id"]].add(r["address_id"])

    uf = UnionFind()
    coinjoin_addrs: set = set()
    for tx_id, addrs in tx_inputs.items():
        if len(addrs) < 2:
            continue
        if is_probable_coinjoin(conn, tx_id):
            coinjoin_addrs |= addrs
        addr_list = sorted(addrs)
        for x in addr_list[1:]:
            uf.union(addr_list[0], x)

    clusters = [c for c in uf.groups() if len(c) >= 2]
    if not clusters:
        return {"clusters": 0, "entities_created": 0, "memberships_created": 0, "merges": 0}

    sq = SourceQuery(connector="cospend-clustering", capability="cluster_cospend", endpoint="local",
                     params={"k_inputs": K_INPUTS, "k_equal_outputs": K_EQUAL_OUTPUTS,
                             "whirlpool_denoms": list(WHIRLPOOL_DENOMS),
                             "confidence": COSPEND_CONFIDENCE},
                     requested_at=now, completed_at=now, status="ok")

    stats = {"clusters": len(clusters), "entities_created": 0, "memberships_created": 0, "merges": 0}

    def write(c, sqid):
        for cluster in clusters:
            existing = set()
            for addr in cluster:
                existing |= _cospend_membership_entities(c, addr)
            if not existing:
                survivor = repo.insert_entity(c, Entity(origin="cospend-cluster"), now=now)
                stats["entities_created"] += 1
            else:
                survivor = min(existing)
                for other in existing:
                    if other != survivor:  # bridge two prior clusters -> merge (decision #3)
                        c.execute("UPDATE entity SET merged_into=? WHERE id=?", (survivor, other))
                        stats["merges"] += 1
            for addr in cluster:
                if survivor in _cospend_membership_entities(c, addr):
                    continue  # already a member (deterministic — insert once)
                flag = "possible-coinjoin" if addr in coinjoin_addrs else None
                conf = COINJOIN_CONFIDENCE if flag else COSPEND_CONFIDENCE
                repo.insert_entity_membership(c, EntityMembership(
                    entity_id=survivor, address_id=addr, source="cospend-heuristic",
                    method="co-spend", confidence=conf, flags=flag), sqid, now=now)
                stats["memberships_created"] += 1
        return stats

    write_with_provenance(conn, sq, write)
    return stats


# --- same-address heuristic (EVM only; low confidence; never across EVM/BTC) -----------------

def link_same_address(conn, *, now: str | None = None) -> dict:
    """Link identical EVM hex addresses across chains into a same-address entity (low confidence)."""
    now = now or utc_now_iso()
    groups: dict = defaultdict(list)
    for r in conn.execute(
        "SELECT id, address, chain FROM address WHERE chain != 'bitcoin'").fetchall():
        groups[r["address"]].append(r["id"])
    multi = {addr: ids for addr, ids in groups.items() if len(ids) >= 2}
    if not multi:
        return {"linked": 0}

    sq = SourceQuery(connector="same-address-heuristic", capability="link_same_address",
                     endpoint="local", params={"confidence": SAME_ADDRESS_CONFIDENCE},
                     requested_at=now, completed_at=now, status="ok")
    stats = {"linked": 0}

    def _same_address_entities(c, address_id):
        return {resolve(c, r["entity_id"]) for r in c.execute(
            "SELECT entity_id FROM entity_membership "
            "WHERE address_id=? AND method='same-address-heuristic'", (address_id,)).fetchall()}

    def write(c, sqid):
        for _addr, ids in multi.items():
            existing = set()
            for aid in ids:
                existing |= _same_address_entities(c, aid)
            survivor = min(existing) if existing else repo.insert_entity(
                c, Entity(origin="investigator", entity_type="same-address"), now=now)
            for aid in ids:
                if survivor in _same_address_entities(c, aid):
                    continue  # already linked (idempotent — insert once per address)
                repo.insert_entity_membership(c, EntityMembership(
                    entity_id=survivor, address_id=aid, source="same-address-heuristic",
                    method="same-address-heuristic", confidence=SAME_ADDRESS_CONFIDENCE), sqid, now=now)
                stats["linked"] += 1
        return stats

    write_with_provenance(conn, sq, write)
    return stats


# --- investigator grouping + merge/split -----------------------------------------------------

def add_investigator_membership(conn, *, entity_id: str, address_id: str, now: str | None = None) -> str:
    """Manually assert an address belongs to an entity (investigator-authored, provenance nullable)."""
    return repo.insert_entity_membership(conn, EntityMembership(
        entity_id=entity_id, address_id=address_id, source="investigator", method="manual"),
        None, now=now or utc_now_iso())


def _is_retracted(conn, membership_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM entity_membership_retraction WHERE membership_id=?", (membership_id,)).fetchone() is not None


def set_canonical_membership(conn, *, entity_id: str, membership_id: str) -> None:
    """Curate the canonical membership for an entity (its app-enforced ref). Validated."""
    canonical = resolve(conn, entity_id)
    m = conn.execute("SELECT entity_id FROM entity_membership WHERE id=?", (membership_id,)).fetchone()
    if m is None or resolve(conn, m["entity_id"]) != canonical:
        raise ValueError("membership does not belong to this entity")
    if _is_retracted(conn, membership_id):
        raise ValueError("cannot set a retracted membership as canonical")
    conn.execute("UPDATE entity SET canonical_membership_id=? WHERE id=?", (membership_id, canonical))


def merge_entities(conn, *, into_id: str, from_id: str) -> str:
    """Merge ``from_id`` into ``into_id`` (tombstone via merged_into). Memberships untouched."""
    target = resolve(conn, into_id)
    if resolve(conn, from_id) == target:
        return target  # already merged
    if target == from_id:
        raise ValueError("cannot merge an entity into one that resolves back to it (cycle)")
    conn.execute("UPDATE entity SET merged_into=? WHERE id=?", (target, from_id))
    return target


def split_address(conn, *, membership_id: str, reason: str, source: str = "investigator",
                  now: str | None = None) -> str:
    """Split an address out of its entity: retract the membership (append-only) + new entity + new
    membership. No membership row is rewritten (round-trips with merge)."""
    now = now or utc_now_iso()
    m = conn.execute(
        "SELECT entity_id, address_id FROM entity_membership WHERE id=?", (membership_id,)).fetchone()
    if m is None:
        raise ValueError(f"membership {membership_id!r} not found")
    repo.insert_entity_membership_retraction(conn, EntityMembershipRetraction(
        membership_id=membership_id, reason=reason, source=source, method="manual"), None, now=now)
    new_entity = repo.insert_entity(conn, Entity(origin="investigator"), now=now)
    repo.insert_entity_membership(conn, EntityMembership(
        entity_id=new_entity, address_id=m["address_id"], source="investigator", method="manual"),
        None, now=now)
    return new_entity
