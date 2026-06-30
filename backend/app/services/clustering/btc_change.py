"""Bitcoin change-address heuristics — faithful to BlockSci 0.7
(https://citp.github.io/BlockSci/reference/heuristics/change.html).

A change heuristic returns, for one transaction, the SET of OUTPUTS it considers *candidate change* (it
does NOT by itself pick exactly one — see ``unique_change``). BlockSci's explicit guidance is quoted in
the module and honored: **"We recommend against simply using one of these heuristics without further
refinement for clustering."** So this module never clusters on a single bare heuristic — it requires the
**agreement of ≥N heuristics** (each first reduced to its single ``unique_change`` candidate), exactly the
documented pattern ``address_reuse.unique_change & optimal_change.unique_change``.

Clustering semantics (Meiklejohn-style common-control extended by change): the identified change output
belongs to the SAME wallet as the transaction's inputs, so a confirmed change links {input addresses} ∪
{change address}. Union-find over those links materialises change-based clusters as a SEPARATE producer
(``origin='heuristic-cluster'``, ``source='btc-change-heuristic'``) sitting SIDE-BY-SIDE with co-spend
(Invariant #4). **CoinJoin gating still applies** — a probable-CoinJoin tx is never used to link addresses
(``is_probable_coinjoin``), so the mixer never bridges two wallets.

Static heuristics (tx-only): address_reuse, address_type, optimal_change, power_of_ten_value(digits),
client_change_address_behavior. Dynamic (need spend data): peeling_chain (uses ``tx_output.spent``);
locktime is DATA-GATED — BIH does not store a tx's nLockTime, so that heuristic is unavailable and
reported as such (never guessed). Composition combinators (& | - and unique_change) mirror BlockSci.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ...db import repository as repo
from ...db.repository import utc_now_iso
from ...models import Entity, EntityMembership, SourceQuery
from ...provenance.atomic import write_with_provenance
from ..entities import UnionFind, is_probable_coinjoin, resolve

# The set of change heuristics this build implements. locktime is intentionally absent (no nLockTime in
# the schema); a caller asking for it is told it's unavailable rather than silently dropped.
STATIC_HEURISTICS = ("address_reuse", "address_type", "optimal_change", "power_of_ten_value",
                     "client_change_address_behavior")
DYNAMIC_HEURISTICS = ("peeling_chain",)
UNAVAILABLE_HEURISTICS = {"locktime": "BIH does not store a transaction's nLockTime (schema has no "
                          "locktime column), so the BlockSci locktime change heuristic is unavailable."}
ALL_HEURISTICS = STATIC_HEURISTICS + DYNAMIC_HEURISTICS

DEFAULT_POWER_OF_TEN_DIGITS = 6  # 10^6 sat = 0.01 BTC — round payment amounts are the spend, not change
HEURISTIC_VERSION = "blocksci-0.7"


# --------------------------------------------------------------------------- per-tx data model

@dataclass
class _Output:
    id: str
    address_id: str | None
    amount: int
    addr: str | None
    addr_type: str | None
    spent: bool
    first_funding: bool  # this output is the FIRST time value lands on its address (within the case)


@dataclass
class _Tx:
    tx_id: str
    inputs: list[tuple[str, int, str | None]]   # (address_id, amount, addr_type)
    outputs: list[_Output]
    coinjoin: bool = False
    peel_change_output_id: str | None = None     # precomputed continuation output (peeling_chain), if any
    _input_addr_ids: set = field(default_factory=set)
    _input_types: set = field(default_factory=set)
    _min_input: int | None = None


def _addr_type(addr: str | None) -> str | None:
    """Coarse Bitcoin output script type from the address encoding (enough for `address_type`)."""
    if not addr:
        return None
    a = addr.lower()
    if a.startswith("bc1p") or a.startswith("tb1p"):
        return "p2tr"
    if a.startswith("bc1") or a.startswith("tb1") or a.startswith("bcrt1"):
        return "p2wpkh" if len(a) <= 45 else "p2wsh"
    if addr.startswith("1") or addr.startswith("m") or addr.startswith("n"):
        return "p2pkh"
    if addr.startswith("3") or addr.startswith("2"):
        return "p2sh"
    return "other"


# --------------------------------------------------------------------------- the heuristics (faithful)

def h_address_reuse(tx: _Tx) -> set[str]:
    """BlockSci: "If input addresses appear as an output address, the client might have reused addresses
    for change." -> outputs whose address is among the inputs."""
    return {o.id for o in tx.outputs if o.address_id is not None and o.address_id in tx._input_addr_ids}


def h_address_type(tx: _Tx) -> set[str]:
    """BlockSci: "If all inputs are of one address type ... the change output has the same type." Only
    fires when the inputs share exactly ONE type; returns outputs of that type."""
    if len(tx._input_types) != 1:
        return set()
    (itype,) = tuple(tx._input_types)
    return {o.id for o in tx.outputs if o.addr_type == itype}


def h_optimal_change(tx: _Tx) -> set[str]:
    """BlockSci: "If there exists an output that is smaller than any of the inputs it is likely the change
    ... (if a transaction has only one input, all outputs are likely to have a value smaller than the
    input and will be returned)." Single-input caveat preserved verbatim."""
    if tx._min_input is None:
        return set()
    if len(tx.inputs) == 1:                       # documented single-input caveat: every output qualifies
        return {o.id for o in tx.outputs}
    return {o.id for o in tx.outputs if o.amount < tx._min_input}


def h_power_of_ten_value(tx: _Tx, digits: int = DEFAULT_POWER_OF_TEN_DIGITS) -> set[str]:
    """BlockSci: "excluding output values that are multiples of 10^digits, as such values are unlikely to
    occur randomly" — round (power-of-ten) outputs are the deliberate spend; the change is among the rest.
    Candidates = outputs whose amount is NOT a multiple of 10^digits."""
    p = 10 ** digits
    return {o.id for o in tx.outputs if o.amount % p != 0}


def h_client_change_address_behavior(tx: _Tx) -> set[str]:
    """BlockSci: "Most clients will generate a fresh address for the change. If an output is the first to
    send value to an address, it is potentially the change." Approximated WITHIN the case (first funding
    of the address among the case's outputs) — a documented within-case caveat."""
    return {o.id for o in tx.outputs if o.first_funding}


def h_peeling_chain(tx: _Tx) -> set[str]:
    """BlockSci (dynamic): "If the transaction is a peeling chain, returns the outputs that continue the
    peeling chain." A peeling chain is a 2-output tx where one (small set of) output is spent onward; the
    spent continuation is the change. Precomputed (needs the spend graph) into ``peel_change_output_id``."""
    return {tx.peel_change_output_id} if tx.peel_change_output_id else set()


_STATIC_FNS = {
    "address_reuse": h_address_reuse,
    "address_type": h_address_type,
    "optimal_change": h_optimal_change,
    "power_of_ten_value": h_power_of_ten_value,
    "client_change_address_behavior": h_client_change_address_behavior,
    "peeling_chain": h_peeling_chain,
}


def candidates(tx: _Tx, name: str, *, power_of_ten_digits: int = DEFAULT_POWER_OF_TEN_DIGITS) -> set[str]:
    fn = _STATIC_FNS[name]
    if name == "power_of_ten_value":
        return fn(tx, power_of_ten_digits)
    return fn(tx)


def unique_change(cands: set[str]) -> set[str]:
    """BlockSci ``unique_change``: "return a single output if it's the only candidate output, and no
    outputs otherwise." The de-noiser the docs require before clustering."""
    return cands if len(cands) == 1 else set()


def compose(tx: _Tx, names: list[str], *, mode: str = "agree", require_agree: int = 2,
            power_of_ten_digits: int = DEFAULT_POWER_OF_TEN_DIGITS) -> set[str]:
    """Combine several heuristics into the FINAL change-output set for ``tx``. Modes mirror BlockSci:
      * ``and``   — intersection (``&``) of the raw candidate sets.
      * ``or``    — union (``|``).
      * ``diff``  — set-difference: names[0] minus the union of the rest (``-``).
      * ``agree`` — the DEFAULT + recommended path: reduce EACH heuristic to its ``unique_change`` candidate,
                    then keep an output only if ≥``require_agree`` heuristics independently chose it. Never a
                    single bare heuristic (BlockSci's explicit guidance)."""
    sets = [candidates(tx, n, power_of_ten_digits=power_of_ten_digits) for n in names]
    if mode == "and":
        out = sets[0].copy() if sets else set()
        for s in sets[1:]:
            out &= s
        return out
    if mode == "or":
        out: set[str] = set()
        for s in sets:
            out |= s
        return out
    if mode == "diff":
        out = sets[0].copy() if sets else set()
        for s in sets[1:]:
            out -= s
        return out
    # agree (default): per-heuristic unique candidate, then count agreement
    votes: dict[str, int] = defaultdict(int)
    for s in sets:
        for oid in unique_change(s):
            votes[oid] += 1
    return {oid for oid, n in votes.items() if n >= require_agree}


# --------------------------------------------------------------------------- load tx data + peel detection

def _load_txs(conn) -> list[_Tx]:
    """Load every Bitcoin transaction with its inputs + outputs, flag CoinJoins, and precompute each
    output's first-funding flag (client_change) + the peeling-chain continuation output."""
    addr = {r["id"]: r["address"] or r["address_display"] for r in
            conn.execute("SELECT id, address, address_display FROM address WHERE chain='bitcoin'").fetchall()}

    inputs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT i.transaction_id, i.address_id, i.amount FROM tx_input i "
        "JOIN transaction_ t ON t.id=i.transaction_id WHERE t.chain='bitcoin' AND i.address_id IS NOT NULL"
    ).fetchall():
        inputs[r["transaction_id"]].append((r["address_id"], int(r["amount"])))

    out_rows: dict[str, list] = defaultdict(list)
    # first-funding: the earliest output (by block height, then output rowid) that funds each address.
    addr_first_output: dict[str, str] = {}
    for r in conn.execute(
        "SELECT o.id, o.transaction_id, o.address_id, o.amount, o.spent, o.output_index, "
        "       t.block_height FROM tx_output o JOIN transaction_ t ON t.id=o.transaction_id "
        "WHERE t.chain='bitcoin' ORDER BY COALESCE(t.block_height, 1<<62), o.output_index"
    ).fetchall():
        out_rows[r["transaction_id"]].append(r)
        if r["address_id"] is not None and r["address_id"] not in addr_first_output:
            addr_first_output[r["address_id"]] = r["id"]

    # peeling chain: a 2-output tx where exactly one output is spent -> that spent output is the
    # continuation (the change leg of the peel). A faithful structural proxy for BlockSci's peel detection.
    peel: dict[str, str] = {}
    for tx_id, rows in out_rows.items():
        if len(rows) == 2:
            spent = [o for o in rows if o["spent"]]
            if len(spent) == 1:
                peel[tx_id] = spent[0]["id"]

    txs = []
    all_tx_ids = set(inputs) | set(out_rows)
    for tx_id in all_tx_ids:
        outs = []
        for o in out_rows.get(tx_id, []):
            a = addr.get(o["address_id"]) if o["address_id"] else None
            outs.append(_Output(
                id=o["id"], address_id=o["address_id"], amount=int(o["amount"]), addr=a,
                addr_type=_addr_type(a), spent=bool(o["spent"]),
                first_funding=(o["address_id"] is not None
                               and addr_first_output.get(o["address_id"]) == o["id"])))
        ins = [(aid, amt, _addr_type(addr.get(aid))) for aid, amt in inputs.get(tx_id, [])]
        tx = _Tx(tx_id=tx_id, inputs=ins, outputs=outs,
                 coinjoin=is_probable_coinjoin(conn, tx_id), peel_change_output_id=peel.get(tx_id))
        tx._input_addr_ids = {aid for aid, _, _ in ins}
        tx._input_types = {t for _, _, t in ins if t}
        tx._min_input = min((amt for _, amt, _ in ins), default=None)
        txs.append(tx)
    return txs


# --------------------------------------------------------------------------- the clustering producer

CHANGE_BASE_CONFIDENCE = 0.4   # a change-link is weaker than co-spend (0.9); agreement raises it
CHANGE_CONFIDENCE_PER_AGREE = 0.15
CHANGE_CONFIDENCE_CAP = 0.85


def _change_confidence(n_agree: int) -> float:
    return round(min(CHANGE_CONFIDENCE_CAP, CHANGE_BASE_CONFIDENCE + CHANGE_CONFIDENCE_PER_AGREE * n_agree), 3)


def preview_change_clusters(conn, *, heuristics: list[str] | None = None, require_agree: int = 2,
                            mode: str = "agree", power_of_ten_digits: int = DEFAULT_POWER_OF_TEN_DIGITS) -> dict:
    """Compute (WITHOUT writing) the change-based clusters: which addresses link, the heuristic config, and
    a per-cluster confidence — so the panel can show what a run WOULD merge before applying."""
    names = [h for h in (heuristics or list(STATIC_HEURISTICS)) if h in _STATIC_FNS]
    txs = _load_txs(conn)
    uf = UnionFind()
    # track, per address, the strongest agreement count that linked it (drives confidence)
    addr_agree: dict[str, int] = defaultdict(int)
    links = 0
    for tx in txs:
        if tx.coinjoin or not tx._input_addr_ids:
            continue  # CoinJoin gating: a mixer never links wallets
        change = compose(tx, names, mode=mode, require_agree=require_agree,
                         power_of_ten_digits=power_of_ten_digits)
        if len(change) != 1:
            continue  # a confident change link needs exactly one identified change output
        (out_id,) = tuple(change)
        change_out = next((o for o in tx.outputs if o.id == out_id), None)
        if change_out is None or change_out.address_id is None:
            continue
        n_agree = 0
        if mode == "agree":
            n_agree = sum(1 for n in names if unique_change(candidates(tx, n, power_of_ten_digits=power_of_ten_digits)) == change)
        members = sorted(tx._input_addr_ids | {change_out.address_id})
        for x in members[1:]:
            uf.union(members[0], x)
        # confidence tracks the ACTUAL agreement count; in and/or/diff modes (no vote tally) fall back to the
        # threshold itself, never to len(names) (which would over-state agreement to the cap).
        for a in members:
            addr_agree[a] = max(addr_agree[a], n_agree or require_agree)
        links += 1
    clusters = [sorted(c) for c in uf.groups() if len(c) >= 2]
    return {"clusters": clusters, "addr_agree": dict(addr_agree), "links": links,
            "heuristics": names, "mode": mode, "require_agree": require_agree,
            "power_of_ten_digits": power_of_ten_digits, "n_clusters": len(clusters)}


def cluster_btc_change(conn, *, heuristics: list[str] | None = None, require_agree: int = 2,
                       mode: str = "agree", power_of_ten_digits: int = DEFAULT_POWER_OF_TEN_DIGITS,
                       now: str | None = None) -> dict:
    """Apply the BlockSci change heuristics and MATERIALISE change-based clusters as a SEPARATE producer
    (origin='heuristic-cluster', source='btc-change-heuristic'), side-by-side with co-spend. Each membership
    carries the run's ``source_query`` (Invariant #3) + a confidence reflecting heuristic agreement, and is
    reversible (split via retraction; undo the whole run by its ``source_query_id``)."""
    now = now or utc_now_iso()
    prev = preview_change_clusters(conn, heuristics=heuristics, require_agree=require_agree, mode=mode,
                                   power_of_ten_digits=power_of_ten_digits)
    clusters = prev["clusters"]
    if not clusters:
        return {"clusters": 0, "entities_created": 0, "memberships_created": 0,
                "heuristics": prev["heuristics"], "source_query_id": None}

    sq = SourceQuery(connector="btc-change-clustering", capability="cluster_btc_change", endpoint="local",
                     params={"heuristics": prev["heuristics"], "mode": mode, "require_agree": require_agree,
                             "power_of_ten_digits": power_of_ten_digits, "version": HEURISTIC_VERSION},
                     requested_at=now, completed_at=now, status="ok")
    stats = {"clusters": len(clusters), "entities_created": 0, "memberships_created": 0,
             "heuristics": prev["heuristics"]}
    addr_agree = prev["addr_agree"]

    def write(c, sqid):
        method = "change:" + "+".join(prev["heuristics"]) + f"@{mode}>={require_agree}"
        for cluster in clusters:
            eid = repo.insert_entity(c, Entity(origin="heuristic-cluster", entity_type="btc-change"), now=now)
            stats["entities_created"] += 1
            for addr_id in cluster:
                conf = _change_confidence(addr_agree.get(addr_id, require_agree))
                repo.upsert_entity_membership(c, EntityMembership(
                    entity_id=eid, address_id=addr_id, source="btc-change-heuristic",
                    method=method, confidence=conf), sqid, now=now)
                stats["memberships_created"] += 1
        return stats

    _, res = write_with_provenance(conn, sq, write)
    res["source_query_id"] = sq.id
    return res
