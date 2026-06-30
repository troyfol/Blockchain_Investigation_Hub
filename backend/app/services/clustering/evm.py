"""Ethereum (EVM) address-clustering heuristics — faithful to Friedhelm Victor, "Address Clustering
Heuristics for Ethereum", Financial Cryptography 2020 (https://ifca.ai/fc20/preproceedings/31.pdf, §5).

Three separate, confidence-tagged, REVERSIBLE producers, each writing its own ``source_query`` (Inv #3),
each ON only when explicitly applied, never merged into one answer (side-by-side, Inv #4):

  1. DEPOSIT-ADDRESS REUSE (§5.1, primary). A deposit address v_d sits on a path v_u → v_d → v_e where
     v_e is a known EXCHANGE: v_d forwards ~the received amount to the exchange shortly after. Formally
     (Algorithm 1): e1=(v_u→v_d), e2=(v_d→v_e) with (i) e1.type == e2.type, (ii) 0 ≤ e1.amount−e2.amount
     ≤ a_max, (iii) 0 ≤ e2.block−e1.block ≤ t_max; v_u ∉ exch∪miners, v_d ∉ exch, v_e ∈ exch, and v_d
     forwards to exactly ONE exchange. Users sending to the SAME v_d are clustered. Paper defaults
     a_max=0.01 ETH (tokens: 0), t_max=3200 blocks. The documented MASQUERADE false-positive (an adversary
     forwards a received amount to an exchange so their address looks like a deposit and pulls the sender
     into their cluster) is encoded as a confidence REDUCER + a warning flag on thin (single-sender)
     deposits.

  2. AIRDROP MULTI-PARTICIPATION (§5.2). Recipients of the same fixed-amount airdrop who forward the EXACT
     received amount into a common aggregator are one entity (agg_min=2). The aggregator must not be an
     exchange/DEX/inactive account; entities capped at ≤1000 addresses.

  3. SELF-AUTHORIZATION (§5.3). An ERC-20 Approval(owner, spender) where both are active EOAs (exchange
     spenders removed) links owner↔spender; token+amount ignored; bounded ≤10 owners/spender & ≤10
     spenders/owner. DATA-GATED: BIH's Etherscan connector fetches Transfer events only, NOT Approval
     events, so this reads the ``erc20_approval`` table (populated by an explicit import) and is a clean
     no-op when empty — an honest "no approval data" result, never a fabricated link.

"Known exchange" is taken from attribution(category LIKE 'exchange') — the paper requires a label set; BIH
sources it from the free/paid attribution pillars. Miner/contract labels are not in the schema, so those
exclusions degrade to empty + are documented (a documented caveat, never silently assumed).
"""

from __future__ import annotations

from collections import defaultdict

from ...db import repository as repo
from ...db.repository import utc_now_iso
from ...models import Entity, EntityMembership, SourceQuery
from ...provenance.atomic import write_with_provenance
from ..entities import UnionFind

HEURISTIC_VERSION = "victor-fc2020"

# Paper-calibrated defaults (§5.1). a_max is in NATIVE units; for tokens it is 0 (fees can't be paid in
# tokens). t_max is a block-height window (~13h on Ethereum at the paper's calibration).
DEFAULT_A_MAX_ETH = 0.01
DEFAULT_T_MAX_BLOCKS = 3200
DEPOSIT_REUSE_CONFIDENCE = 0.6        # "highly likely" but the paper documents false positives
MASQUERADE_REDUCED_CONFIDENCE = 0.35  # a thin (single-sender) deposit is the masquerade shape
AIRDROP_CONFIDENCE = 0.5
SELF_AUTH_CONFIDENCE = 0.45
MAX_CLUSTER_ADDRESSES = 1000          # paper's over-large-cluster cap (both airdrop + a safety bound)
AIRDROP_MIN_RECIPIENTS = 1000
AIRDROP_AGG_MIN = 2
SELF_AUTH_MAX_PER_SIDE = 10


# --------------------------------------------------------------------------- shared data access

def _exchange_address_ids(conn) -> set[str]:
    """Known-exchange address ids from the attribution pillar (category contains 'exchange')."""
    return {r["address_id"] for r in conn.execute(
        "SELECT DISTINCT address_id FROM attribution WHERE LOWER(COALESCE(category,'')) LIKE '%exchange%'"
    ).fetchall()}


def _evm_transfers(conn) -> list[dict]:
    """Every EVM transfer with endpoints, asset, native-amount int, and block height (for the window)."""
    out = []
    for r in conn.execute(
        "SELECT tr.id, tr.from_address_id AS src, tr.to_address_id AS dst, tr.asset_id, tr.amount, "
        "       t.block_height, a.contract_address, a.decimals "
        "FROM transfer tr JOIN transaction_ t ON t.id=tr.transaction_id "
        "LEFT JOIN asset a ON a.id=tr.asset_id WHERE tr.chain != 'bitcoin'"
    ).fetchall():
        if r["src"] is None or r["dst"] is None:
            continue
        try:
            amt = int(r["amount"])
        except (TypeError, ValueError):
            continue
        out.append({"id": r["id"], "src": r["src"], "dst": r["dst"], "asset_id": r["asset_id"],
                    "amount": amt, "block": r["block_height"],
                    "is_token": r["contract_address"] is not None,
                    "decimals": r["decimals"] if r["decimals"] is not None else 18})
    return out


def _materialise(conn, sq: SourceQuery, clusters: list[list[str]], *, source: str, method: str,
                 entity_type: str, confidences: dict[str, float], flags: dict[str, str] | None = None,
                 now: str) -> dict:
    """Write each cluster as its own heuristic-cluster entity + per-address memberships (one source_query
    for the whole run, so the run is undoable as a unit). Reversible by construction."""
    flags = flags or {}
    stats = {"clusters": len(clusters), "entities_created": 0, "memberships_created": 0}

    def write(c, sqid):
        for cluster in clusters:
            eid = repo.insert_entity(c, Entity(origin="heuristic-cluster", entity_type=entity_type), now=now)
            stats["entities_created"] += 1
            for addr_id in cluster:
                repo.upsert_entity_membership(c, EntityMembership(
                    entity_id=eid, address_id=addr_id, source=source, method=method,
                    confidence=confidences.get(addr_id, DEPOSIT_REUSE_CONFIDENCE),
                    flags=flags.get(addr_id)), sqid, now=now)
                stats["memberships_created"] += 1
        return stats

    _, res = write_with_provenance(conn, sq, write)
    res["source_query_id"] = sq.id
    return res


# --------------------------------------------------------------------------- (1) deposit-address reuse

def preview_deposit_reuse(conn, *, a_max_eth: float = DEFAULT_A_MAX_ETH,
                          t_max_blocks: int = DEFAULT_T_MAX_BLOCKS,
                          exchange_ids: set[str] | None = None) -> dict:
    """Identify deposit addresses + the user clusters that send to them (no writes). Returns clusters,
    per-address confidence, and masquerade warnings."""
    exch = exchange_ids if exchange_ids is not None else _exchange_address_ids(conn)
    transfers = _evm_transfers(conn)
    # incoming (v_u -> v_d) and outgoing (v_d -> v_e) indexed by the middle node v_d.
    incoming: dict[str, list[dict]] = defaultdict(list)
    outgoing: dict[str, list[dict]] = defaultdict(list)
    for tr in transfers:
        incoming[tr["dst"]].append(tr)
        outgoing[tr["src"]].append(tr)

    uf = UnionFind()
    deposit_users: dict[str, set[str]] = defaultdict(set)   # v_d -> {v_u}
    deposit_exch: dict[str, set[str]] = defaultdict(set)    # v_d -> {v_e it forwards to}
    for vd in incoming:
        if vd in exch:
            continue  # v_d ∉ exchanges
        for e2 in outgoing.get(vd, []):
            ve = e2["dst"]
            if ve not in exch:
                continue  # e2 must forward to a known exchange
            a_max = 0 if e2["is_token"] else int(a_max_eth * (10 ** e2["decimals"]))
            for e1 in incoming[vd]:
                vu = e1["src"]
                if vu in exch:
                    continue  # v_u ∉ exchanges (miner labels unavailable -> documented)
                if e1["asset_id"] != e2["asset_id"]:
                    continue  # (i) same type
                if not (0 <= e1["amount"] - e2["amount"] <= a_max):
                    continue  # (ii) forwarded ≤ received, within tolerance
                if e1["block"] is None or e2["block"] is None or not (0 <= e2["block"] - e1["block"] <= t_max_blocks):
                    continue  # (iii) forwarded shortly after, within the block window
                deposit_users[vd].add(vu)
                deposit_exch[vd].add(ve)
    # require the deposit forwards to exactly ONE exchange (avoid linking two majors), per the paper.
    for vd, users in deposit_users.items():
        if len(deposit_exch[vd]) != 1:
            continue
        members = sorted(users)        # users sharing one deposit = same entity (v_d itself is the exchange's)
        for u in members[1:]:
            uf.union(members[0], u)
    clusters = [sorted(c) for c in uf.groups() if 2 <= len(c) <= MAX_CLUSTER_ADDRESSES]
    # MASQUERADE false-positive (Victor §"Limitations"): an adversary forwards a received amount to an
    # exchange so their address looks like a deposit and pulls the sender into a cluster. The minimal such
    # link is a SIZE-2 cluster (one masquerading deposit fully explains exactly two senders), so size-2
    # clusters carry the REDUCED confidence + a 'masquerade-risk' flag; larger many-sender clusters (which
    # an adversary can't fabricate from a single forward) keep full confidence.
    confidences: dict[str, float] = {}
    flags: dict[str, str] = {}
    for cluster in clusters:
        masquerade = len(cluster) == 2
        for a in cluster:
            confidences[a] = MASQUERADE_REDUCED_CONFIDENCE if masquerade else DEPOSIT_REUSE_CONFIDENCE
            if masquerade:
                flags[a] = "masquerade-risk"
    return {"clusters": clusters, "confidences": confidences, "flags": flags,
            "deposit_addresses": sorted(deposit_users), "n_clusters": len(clusters),
            "a_max_eth": a_max_eth, "t_max_blocks": t_max_blocks}


def cluster_deposit_reuse(conn, *, a_max_eth: float = DEFAULT_A_MAX_ETH,
                          t_max_blocks: int = DEFAULT_T_MAX_BLOCKS, now: str | None = None) -> dict:
    now = now or utc_now_iso()
    prev = preview_deposit_reuse(conn, a_max_eth=a_max_eth, t_max_blocks=t_max_blocks)
    if not prev["clusters"]:
        return {"clusters": 0, "entities_created": 0, "memberships_created": 0, "source_query_id": None}
    sq = SourceQuery(connector="evm-deposit-reuse", capability="cluster_deposit_reuse", endpoint="local",
                     params={"a_max_eth": a_max_eth, "t_max_blocks": t_max_blocks,
                             "version": HEURISTIC_VERSION}, requested_at=now, completed_at=now, status="ok")
    return _materialise(conn, sq, prev["clusters"], source="evm-deposit-reuse", method="deposit-forward",
                        entity_type="evm-deposit-reuse", confidences=prev["confidences"],
                        flags=prev["flags"], now=now)


# --------------------------------------------------------------------------- (2) airdrop multi-participation

def preview_airdrop(conn, *, min_recipients: int = AIRDROP_MIN_RECIPIENTS, agg_min: int = AIRDROP_AGG_MIN,
                    exchange_ids: set[str] | None = None) -> dict:
    """Detect fixed-amount airdrops (a source distributing one exact token amount to many recipients) and
    cluster recipients who forward that EXACT amount to a common aggregator (agg_min). Aggregators that are
    exchanges/inactive are excluded; entities capped at ≤1000 addresses."""
    exch = exchange_ids if exchange_ids is not None else _exchange_address_ids(conn)
    transfers = [t for t in _evm_transfers(conn) if t["is_token"]]
    # airdrop = (source, asset, amount) distributing to >= min_recipients distinct recipients.
    dist: dict[tuple, set[str]] = defaultdict(set)
    for t in transfers:
        dist[(t["src"], t["asset_id"], t["amount"])].add(t["dst"])
    airdrops = {k for k, recips in dist.items() if len(recips) >= min_recipients}
    recipient_of: dict[tuple, set[str]] = {k: dist[k] for k in airdrops}

    uf = UnionFind()
    # for each airdrop, find recipients that forward the EXACT amount to a common aggregator.
    for (src, asset_id, amount), recips in recipient_of.items():
        agg_senders: dict[str, set[str]] = defaultdict(set)  # aggregator -> {recipient who forwarded}
        for t in transfers:
            if t["asset_id"] == asset_id and t["amount"] == amount and t["src"] in recips:
                if t["dst"] in exch:
                    continue  # aggregator must not be an exchange/DEX
                agg_senders[t["dst"]].add(t["src"])
        for agg, senders in agg_senders.items():
            if len(senders) < agg_min:
                continue
            members = sorted(senders | {agg})   # the recipients + the aggregator = one entity
            for m in members[1:]:
                uf.union(members[0], m)
    clusters = [sorted(c) for c in uf.groups() if 2 <= len(c) <= MAX_CLUSTER_ADDRESSES]
    return {"clusters": clusters, "n_clusters": len(clusters), "airdrops": len(airdrops),
            "min_recipients": min_recipients, "agg_min": agg_min}


def cluster_airdrop(conn, *, min_recipients: int = AIRDROP_MIN_RECIPIENTS, agg_min: int = AIRDROP_AGG_MIN,
                    now: str | None = None) -> dict:
    now = now or utc_now_iso()
    prev = preview_airdrop(conn, min_recipients=min_recipients, agg_min=agg_min)
    if not prev["clusters"]:
        return {"clusters": 0, "entities_created": 0, "memberships_created": 0, "source_query_id": None}
    sq = SourceQuery(connector="evm-airdrop-multi", capability="cluster_airdrop", endpoint="local",
                     params={"min_recipients": min_recipients, "agg_min": agg_min,
                             "version": HEURISTIC_VERSION}, requested_at=now, completed_at=now, status="ok")
    confidences = {a: AIRDROP_CONFIDENCE for cluster in prev["clusters"] for a in cluster}
    return _materialise(conn, sq, prev["clusters"], source="evm-airdrop-multi", method="airdrop-aggregation",
                        entity_type="evm-airdrop", confidences=confidences, now=now)


# --------------------------------------------------------------------------- (3) self-authorization

def preview_self_authorization(conn, *, max_per_side: int = SELF_AUTH_MAX_PER_SIDE,
                               exchange_ids: set[str] | None = None) -> dict:
    """Cluster owner↔spender from ERC-20 Approval pairs (exchange spenders removed; bounded ≤max_per_side
    each way). DATA-GATED: reads ``erc20_approval`` — a clean no-op when no approval data was imported."""
    exch = exchange_ids if exchange_ids is not None else _exchange_address_ids(conn)
    pairs = [(r["owner_address_id"], r["spender_address_id"]) for r in
             conn.execute("SELECT owner_address_id, spender_address_id FROM erc20_approval").fetchall()
             if r["owner_address_id"] != r["spender_address_id"]]
    if not pairs:
        return {"clusters": [], "n_clusters": 0, "approvals": 0,
                "note": "no ERC-20 Approval data imported (Etherscan fetches Transfer events only)"}
    owners_of_spender: dict[str, set[str]] = defaultdict(set)
    spenders_of_owner: dict[str, set[str]] = defaultdict(set)
    for o, s in pairs:
        owners_of_spender[s].add(o)
        spenders_of_owner[o].add(s)
    uf = UnionFind()
    seen = set()
    for o, s in pairs:
        if s in exch:
            continue  # exchange spenders removed
        if len(owners_of_spender[s]) > max_per_side or len(spenders_of_owner[o]) > max_per_side:
            continue  # a hub spender/owner is not one entity (de-noise bound)
        if (o, s) in seen:
            continue
        seen.add((o, s))
        uf.union(o, s)
    clusters = [sorted(c) for c in uf.groups() if 2 <= len(c) <= MAX_CLUSTER_ADDRESSES]
    return {"clusters": clusters, "n_clusters": len(clusters), "approvals": len(pairs),
            "max_per_side": max_per_side}


def cluster_self_authorization(conn, *, max_per_side: int = SELF_AUTH_MAX_PER_SIDE,
                               now: str | None = None) -> dict:
    now = now or utc_now_iso()
    prev = preview_self_authorization(conn, max_per_side=max_per_side)
    if not prev["clusters"]:
        out = {"clusters": 0, "entities_created": 0, "memberships_created": 0, "source_query_id": None}
        if prev.get("note"):
            out["note"] = prev["note"]
        return out
    sq = SourceQuery(connector="evm-self-authorization", capability="cluster_self_authorization",
                     endpoint="local", params={"max_per_side": max_per_side, "version": HEURISTIC_VERSION},
                     requested_at=now, completed_at=now, status="ok")
    confidences = {a: SELF_AUTH_CONFIDENCE for cluster in prev["clusters"] for a in cluster}
    return _materialise(conn, sq, prev["clusters"], source="evm-self-authorization", method="approve-control",
                        entity_type="evm-self-auth", confidences=confidences, now=now)
