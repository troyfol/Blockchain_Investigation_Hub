"""Focused, scale-aware VIEW over the truthful read model (services/graph.py::build_graph).

A dense case (e.g. a high-degree address with tens of thousands of dust inbounds) is unreadable if the
whole graph is rendered at once. This module wraps ``build_graph`` and returns a BOUNDED view that:

- **focuses** on one node (the seed/anchor by default, else the highest-degree address) and walks
  outward ``hops`` edges, capped at ``node_cap`` nodes — so a view never explodes;
- **aggregates dust / high fan-in**: a focus node's many small/unflagged counterparties collapse into a
  single summary node + edge per direction ("12,431 inflows · $0.43 · dust"), expandable on demand;
- carries **scale-aware honesty** ``meta`` ("displaying N of M (bounded)").

INVARIANTS. This is **display-only over real facts** — it never writes a row and never invents a
fact/edge (Inv #5). An aggregate is a view artifact whose ``underlying`` lists the real counterparties
it stands for, so it is always **expandable to the real underlying** (each of which keeps its own
provenance, Inv #3). Risk/entity/seed/FIFO encodings + valuation come straight from ``build_graph``.
"""

from __future__ import annotations

from collections import defaultdict, deque

from .graph import _usd, aggregate_parallel_edges, build_graph, scale_edge_widths

_REAL_KINDS = ("address", "transaction", "external")

# On EXPAND, reveal at most this many of a bundle's members at once (the rest roll into a ":more"
# residual bundle) — the canvas analogue of the side panel's "top N of M" (P8.6 #5).
EXPAND_REVEAL_CAP = 50

# Denomination grouping (P8.6 #7): a pool of >= this many counterparties sharing ONE exact native
# denomination (e.g. Tornado's 100/10/1/0.1 ETH pools) is clustered into a labeled compound node.
MIN_DENOM_POOL = 2


def _default_focus(g: dict) -> str | None:
    """The seed/anchor if present, else the highest-degree address (the densest, most useful entry)."""
    seed = next((n["id"] for n in g["nodes"] if n.get("seed")), None)
    if seed:
        return seed
    deg: dict[str, int] = defaultdict(int)
    for e in g["edges"]:
        deg[e["source"]] += 1
        deg[e["target"]] += 1
    addrs = [(deg[n["id"]], n["id"]) for n in g["nodes"] if n["kind"] == "address"]
    return max(addrs)[1] if addrs else None


def _flagged(node: dict) -> bool:
    """A counterparty worth keeping out of the dust bucket regardless of value (the intelligence)."""
    return bool(node.get("risk_level") or node.get("has_attribution") or node.get("coinjoin"))


def _agg_node(aggid: str, hub: str, direction: str, members: list[tuple[str, dict]]) -> dict:
    """A display-only summary node standing in for ``members`` (counterparty, edge) of ``hub``."""
    count = len({o for o, _ in members})
    total_usd = round(sum((e.get("value_usd") or 0.0) for _, e in members), 2)
    no_price = sum(1 for _, e in members if not e.get("value_usd"))
    word = "inflows" if direction == "in" else "outflows"
    line2 = []
    if total_usd > 0:
        line2.append(_usd(total_usd))
    if no_price:
        line2.append(f"{no_price:,} no price")
    line2.append("dust")
    return {
        "id": aggid, "kind": "aggregate", "agg_of": hub, "agg_direction": direction,
        "label": f"{count:,} {word}\n" + " · ".join(line2),
        "count": count, "total_usd": total_usd or None, "no_price_count": no_price,
        # provenance pointer: the REAL counterparties this stands for (expandable to the underlying).
        "underlying": sorted({o for o, _ in members}),
        "underlying_edges": sorted({e["id"] for _, e in members}),
    }


def _agg_edge(aggid: str, hub: str, direction: str, total_usd: float | None) -> dict:
    src, tgt = (aggid, hub) if direction == "in" else (hub, aggid)
    return {"id": f"{aggid}::edge", "source": src, "target": tgt, "kind": "aggregate",
            "paradigm": "aggregate", "value_usd": total_usd, "value_usd_label": _usd(total_usd),
            "ew": 4.0, "aggregate": True}


def _user_agg_node(aggid: str, hub: str, direction: str, members: list[tuple[str, dict]],
                   threshold: float) -> dict:
    """The P3.5 VALUE-FILTER bucket (``user_dust``): priced movements the investigator chose to fold
    below a USD threshold. A display-only summary like the auto-dust node, but a DISTINCT kind + label
    (``below $X``) + token so it never reads as — or merges with — the automatic dust aggregate."""
    count = len({o for o, _ in members})
    total_usd = round(sum((e.get("value_usd") or 0.0) for _, e in members), 2)
    word = "inflows" if direction == "in" else "outflows"
    line2 = f"below {_usd(threshold)}"
    if total_usd > 0:
        line2 += f" · {_usd(total_usd)}"
    return {
        "id": aggid, "kind": "user_dust", "agg_of": hub, "agg_direction": direction,
        "label": f"{count:,} {word}\n{line2}",
        "count": count, "total_usd": total_usd or None, "threshold_usd": threshold,
        "underlying": sorted({o for o, _ in members}),
        "underlying_edges": sorted({e["id"] for _, e in members}),
    }


def _user_agg_edge(aggid: str, hub: str, direction: str, total_usd: float | None) -> dict:
    src, tgt = (aggid, hub) if direction == "in" else (hub, aggid)
    return {"id": f"{aggid}::edge", "source": src, "target": tgt, "kind": "user_dust",
            "paradigm": "aggregate", "value_usd": total_usd, "value_usd_label": _usd(total_usd),
            "ew": 4.0, "aggregate": True, "user_dust": True}


def _kind_agg_node(aggid: str, hub: str, direction: str, members: list[tuple[str, dict]], *,
                   kind: str, title: str) -> dict:
    """A generic summary node (P8.7) for the UNVERIFIED-token fold and the address-POISONING fold — its
    own ``kind`` + token so it reads distinctly and never merges with the dust / value-filter buckets. A
    display de-emphasis, NOT a fact/claim about the underlying (expandable to the real underlying)."""
    count = len({o for o, _ in members})
    total_usd = round(sum((e.get("value_usd") or 0.0) for _, e in members), 2)
    word = "inflows" if direction == "in" else "outflows"
    return {
        "id": aggid, "kind": kind, "agg_of": hub, "agg_direction": direction,
        "label": f"{count:,} {word}\n{title}",
        "count": count, "total_usd": total_usd or None,
        "underlying": sorted({o for o, _ in members}),
        "underlying_edges": sorted({e["id"] for _, e in members}),
    }


def _kind_agg_edge(aggid: str, hub: str, direction: str, kind: str) -> dict:
    src, tgt = (aggid, hub) if direction == "in" else (hub, aggid)
    return {"id": f"{aggid}::edge", "source": src, "target": tgt, "kind": kind,
            "paradigm": "aggregate", "ew": 3.0, "aggregate": True}


def _mag(e: dict, basis: str) -> float | None:
    """A fact edge's COMPARABLE magnitude under the active basis, for the dust / value-filter decisions:

      * ``usd``: the USD value-at-time — or ``None`` when UNPRICED. A ``None`` magnitude can't be compared
        to a USD threshold, so the caller KEEPS it (never auto-dusts / folds an unpriced movement just
        because DeFiLlama lacked a price — the P8.6 "unpriced ≠ dust" rule).
      * ``native``: the native amount (every movement has one) — so native mode thresholds + ranks every
        edge by its native magnitude (per-asset; the caller never compares across assets on one scale)."""
    if basis == "native":
        return e.get("value_num")
    return e.get("value_usd")


def build_view(conn, *, focus: str | None = None, hops: int = 1, node_cap: int = 150,
               group_dust: bool = True, dust_floor_usd: float = 1.0, dust_floor_native: float = 0.001,
               value_floor_usd: float = 0.0,
               edge_kinds: list[str] | None = None, only_flagged: bool = False,
               user_dust_usd: float | None = None, expand: tuple[str, ...] = (),
               value_basis: str = "usd", group_denominations: bool = False,
               show_unverified: bool = False, fold_poison: bool = True,
               denom_filters: dict[str, dict] | None = None,
               aggregate_parallel: bool = True, community_detect: bool = False) -> dict:
    basis = "native" if value_basis == "native" else "usd"
    # P8.7 #1 — per-DENOMINATION (per-asset) min/fold thresholds in NATIVE units, so folding the long tail
    # inside one pool (e.g. 5,000,000 cDAI) never touches another (100,000 DAI). asset_symbol -> {min,fold}.
    denom_filters = denom_filters or {}

    def _denom_min(e: dict) -> float | None:
        f = denom_filters.get(e.get("asset_symbol") or "")
        return f.get("min") if f else None

    def _denom_fold(e: dict) -> float | None:
        f = denom_filters.get(e.get("asset_symbol") or "")
        return f.get("fold") if f else None

    def _below_dust(e: dict) -> bool:
        """Is this edge AUTO-dust-small? PRICED edges are judged by USD (``dust_floor_usd``); UNPRICED
        edges fall back to their NATIVE amount vs ``dust_floor_native`` (P8.6 "unpriced ≠ dust" — a small
        no-price movement still dusts, but a LARGE one, e.g. 100 ETH, never does just for lacking a USD
        price). In native mode EVERY edge is judged by native amount (per-asset numeric floor)."""
        if basis == "native":
            return (e.get("value_num") or 0.0) < dust_floor_native
        v = e.get("value_usd")
        if v is not None:
            return v < dust_floor_usd
        return (e.get("value_num") or 0.0) < dust_floor_native

    # Raw INDIVIDUAL edges (aggregate=False): build_view needs per-movement edges for its dust / poison /
    # counterparty folding; it collapses the kept parallels itself at the end (P8.7.3 #3).
    g = build_graph(conn, aggregate=False)
    nodes = {n["id"]: n for n in g["nodes"]}
    total = sum(1 for n in g["nodes"] if n["kind"] in _REAL_KINDS)
    expand_set = set(expand)

    # Resolve the focus: a node id, OR an ADDRESS string (the search/center box) — exact then prefix,
    # case-insensitive — else fall back to the seed/anchor (or the densest address).
    if focus is not None and focus not in nodes:
        q = focus.strip().lower()
        match = next((n["id"] for n in g["nodes"] if (n.get("address") or "").lower() == q), None)
        if match is None:
            match = next((n["id"] for n in g["nodes"] if (n.get("address") or "").lower().startswith(q) and q), None)
        focus = match
    if focus not in nodes:
        focus = _default_focus(g)
    if focus is None:  # empty case — nothing to focus; return the (tiny) full graph with honest meta
        g["meta"] = {"focus": None, "displayed": len(g["nodes"]), "total": total,
                     "bounded": False, "aggregated": 0, "hops": hops, "node_cap": node_cap}
        return g

    # --- edge filters (value floor, edge kinds) — trace overlays always kept ---
    # The "min" floor is interpreted in the ACTIVE basis (P8.6): USD value in usd mode, native amount in
    # native mode. An UNPRICED edge has no USD magnitude, so in usd mode it is KEPT (never filtered out by
    # a USD floor — its honest no-price gap is preserved); in native mode it is filtered by native amount.
    def edge_ok(e: dict) -> bool:
        if e["kind"] == "trace":
            return True
        if edge_kinds and e["kind"] not in edge_kinds:
            return False
        if value_floor_usd:
            m = _mag(e, basis)
            if m is not None and m < value_floor_usd:
                return False
        # P8.7 #1 — a per-denomination MIN (native) drops this asset's sub-threshold edges only.
        dmin = _denom_min(e)
        if dmin and (e.get("value_num") or 0.0) < dmin:
            return False
        return True

    in_adj: dict[str, list[dict]] = defaultdict(list)
    out_adj: dict[str, list[dict]] = defaultdict(list)
    for e in g["edges"]:
        if edge_ok(e):
            in_adj[e["target"]].append(e)
            out_adj[e["source"]].append(e)

    kept: set[str] = {focus}
    kept_edges: dict[str, dict] = {}
    agg_nodes: list[dict] = []
    agg_edges: list[dict] = []
    bounded = False
    aggregated = 0
    depth = {focus: 0}
    queue: deque[str] = deque([focus])

    while queue:
        hub = queue.popleft()
        d = depth[hub]
        if d >= hops:
            continue
        for direction in ("in", "out"):
            incident = (in_adj if direction == "in" else out_adj).get(hub, [])
            cps = [(e["source"] if direction == "in" else e["target"], e) for e in incident]
            cps = [(o, e) for o, e in cps if o != hub]  # drop self-loops
            if not cps:
                continue

            significant: list[tuple[str, dict]] = []
            dust: list[tuple[str, dict]] = []
            user_dust_by_asset: dict[str, list[tuple[str, dict]]] = defaultdict(list)
            unverified: list[tuple[str, dict]] = []
            poison: list[tuple[str, dict]] = []
            for other, e in cps:
                onode = nodes.get(other, {})
                flagged = _flagged(onode) or e["kind"] == "trace"  # FIFO overlay / intelligence always shows
                if not flagged:
                    # P8.7 #2 — an UNVERIFIED token (unpriced ERC-20 not on the allowlist) folds into the
                    # "unverified / unpriced tokens" bucket BY DEFAULT, so airdrop/poison spam (huge native,
                    # no price) doesn't outrank real flows. A display de-emphasis (reveal with
                    # show_unverified), NEVER a claim the token is malicious.
                    if e.get("token_unverified") and not show_unverified:
                        unverified.append((other, e))
                        continue
                    # P8.7 #3 — a poison-suspect (0-value look-alike) transfer folds into its own bucket.
                    if e.get("poison_suspect") and fold_poison:
                        poison.append((other, e))
                        continue
                # AUTO dust (P8.6): small/unflagged by USD (priced) or native (unpriced) magnitude. NOTE:
                # transaction routing nodes are NOT auto-kept by being a tx — a genesis with tens of
                # thousands of dust inbound *txs* must still aggregate those.
                if group_dust and not flagged and _below_dust(e):
                    dust.append((other, e))
                    continue
                # VALUE FILTER: a per-DENOMINATION fold (native, #1) takes precedence over the global fold;
                # both route to a PER-ASSET user_dust bucket so folding one pool never touches another.
                if not flagged:
                    dfold = _denom_fold(e)
                    if dfold and (e.get("value_num") or 0.0) < dfold:
                        user_dust_by_asset[e.get("asset_symbol") or ""].append((other, e))
                        continue
                    m = _mag(e, basis)
                    if user_dust_usd and m is not None and m < user_dust_usd:
                        user_dust_by_asset[""].append((other, e))
                        continue
                significant.append((other, e))

            if only_flagged:  # filter: collapse non-flagged significant into dust too
                still, moved = [], []
                for other, e in significant:
                    (still if (_flagged(nodes.get(other, {})) or e["kind"] == "trace") else moved).append((other, e))
                significant, dust = still, dust + moved

            # Cap kept counterparties to the top by the CURRENT basis; the overflow becomes dust. Unpriced
            # edges (usd mode) are ranked into a HIGHER tier so the cap never dusts a potentially-large
            # no-price movement (#2/#5); within a tier, by magnitude.
            def _cap_rank(oe: tuple[str, dict]) -> tuple[int, float]:
                e = oe[1]
                if basis == "native":
                    return (0, e.get("value_num") or 0.0)
                v = e.get("value_usd")
                return (0, v) if v is not None else (1, e.get("value_num") or 0.0)
            significant.sort(key=_cap_rank, reverse=True)
            room = max(0, node_cap - len(kept))
            if len(significant) > room:
                dust += significant[room:]
                significant = significant[:room]
                bounded = True

            def _keep(other: str, e: dict) -> None:
                if other not in kept:
                    kept.add(other)
                    depth.setdefault(other, d + 1)
                    if nodes.get(other, {}).get("kind") in ("address", "transaction") and d + 1 < hops:
                        queue.append(other)
                kept_edges[e["id"]] = e

            for other, e in significant:
                _keep(other, e)

            # Reveal an EXPANDED bundle's members capped at the top-N by the CURRENT basis; the remainder
            # rolls into a ":more" residual bundle (#5 — expanding a 2k-neighbor hairball must not explode;
            # the canvas honors the same cap the side panel's "top N of M" does). Clicking the ":more"
            # bundle reveals the rest (up to node_cap).
            def _reveal_capped(members: list[tuple[str, dict]], base_id: str, agg_factory) -> None:
                nonlocal bounded, aggregated
                more_id = f"{base_id}:more"
                show_rest = more_id in expand_set
                ordered = sorted(members, key=_cap_rank, reverse=True)
                room = max(0, node_cap - len(kept))
                cap = room if show_rest else min(EXPAND_REVEAL_CAP, room)
                revealed, residual = ordered[:cap], ordered[cap:]
                for other, e in revealed:
                    _keep(other, e)
                if residual:
                    bounded = True
                    aggregated += 1
                    rnode = agg_factory(more_id, hub, direction, residual)
                    n_more = len({o for o, _ in residual})
                    rnode["label"] = f"{n_more:,} more\nclick to show more"
                    rnode["is_more"] = True
                    agg_nodes.append(rnode)
                    agg_edges.append(_agg_edge(more_id, hub, direction, rnode.get("total_usd")))

            aggid = f"agg:{hub}:{direction}"
            if dust and group_dust and aggid not in expand_set:
                bounded = True
                aggregated += 1
                node = _agg_node(aggid, hub, direction, dust)
                agg_nodes.append(node)
                agg_edges.append(_agg_edge(aggid, hub, direction, node["total_usd"]))
            elif dust:  # grouping off OR this aggregate was expanded -> reveal top-N (+ residual ":more")
                _reveal_capped(dust, aggid, _agg_node)

            # P3.5 user_dust (value filter): its OWN aggregate(s), NEVER merged with the auto dust bucket.
            # P8.7 #1: one bucket PER ASSET — the global fold (asset "") keeps the "below $X" label; a
            # per-denomination fold gets a native "below X <ASSET>" label, so each pool folds independently.
            for asset, members in user_dust_by_asset.items():
                if not members:
                    continue
                udid = f"udust:{hub}:{direction}:{asset}" if asset else f"udust:{hub}:{direction}"
                if asset:
                    thr = _denom_fold({"asset_symbol": asset})
                    title = f"below {thr:g} {asset}"
                    factory = (lambda i, h, di, mem, _t=title:
                               _kind_agg_node(i, h, di, mem, kind="user_dust", title=_t))
                else:
                    factory = lambda i, h, di, mem: _user_agg_node(i, h, di, mem, user_dust_usd)  # noqa: E731
                if udid not in expand_set:
                    bounded = True
                    aggregated += 1
                    unode = factory(udid, hub, direction, members)
                    agg_nodes.append(unode)
                    agg_edges.append(_user_agg_edge(udid, hub, direction, unode.get("total_usd")))
                else:  # expanded -> reveal top-N (+ residual ":more")
                    _reveal_capped(members, udid, factory)

            # P8.7 #2 — the UNVERIFIED-tokens bucket (collapsed airdrop/poison spam; its own kind + token).
            uvid = f"unverified:{hub}:{direction}"
            uv_factory = (lambda i, h, di, mem:
                          _kind_agg_node(i, h, di, mem, kind="unverified", title="unverified / unpriced tokens"))
            if unverified and uvid not in expand_set:
                bounded = True
                aggregated += 1
                unode = uv_factory(uvid, hub, direction, unverified)
                agg_nodes.append(unode)
                agg_edges.append(_kind_agg_edge(uvid, hub, direction, "unverified"))
            elif unverified:
                _reveal_capped(unverified, uvid, uv_factory)

            # P8.7 #3 — the POSSIBLE-ADDRESS-POISONING bucket (folded 0-value look-alike transfers).
            pid = f"poison:{hub}:{direction}"
            p_factory = (lambda i, h, di, mem:
                         _kind_agg_node(i, h, di, mem, kind="poison", title="possible address-poisoning"))
            if poison and pid not in expand_set:
                bounded = True
                aggregated += 1
                pnode = p_factory(pid, hub, direction, poison)
                agg_nodes.append(pnode)
                agg_edges.append(_kind_agg_edge(pid, hub, direction, "poison"))
            elif poison:
                _reveal_capped(poison, pid, p_factory)

    # Trace overlays between two kept nodes (the FIFO/investigator convention) always render.
    for e in g["edges"]:
        if e["kind"] == "trace" and e["source"] in kept and e["target"] in kept:
            kept_edges[e["id"]] = e

    # Include compound-group parents for any kept child (Cytoscape needs the parent node present).
    out_nodes: list[dict] = []
    parents_needed: set[str] = set()
    for nid in kept:
        n = nodes.get(nid)
        if n is None:
            continue
        out_nodes.append(n)
        if n.get("parent"):
            parents_needed.add(n["parent"])
    for pid in parents_needed:
        if pid in nodes and pid not in kept:
            out_nodes.append(nodes[pid])
    out_nodes.extend(agg_nodes)

    # DENOMINATION GROUPING (#7): cluster the FOCUS's kept counterparties that share ONE exact native
    # denomination (e.g. many addresses each receiving exactly 100 ETH — a mixer pool) into a labeled
    # compound node, the natural cousin of the co-spend/entity grouping. Per-asset (an "equal denomination"
    # only means anything within one asset). Skips counterparties that already have a parent (co-spend /
    # entity wins). A view artifact — never a fact (Inv #5).
    denom_groups = 0
    if group_denominations and focus is not None:
        node_by_id = {n["id"]: n for n in out_nodes}
        buckets: dict[tuple, list[str]] = defaultdict(list)
        bucket_label: dict[tuple, str] = {}
        for e in kept_edges.values():
            if e["kind"] not in ("transfer", "tx_input", "tx_output"):
                continue
            other = e["target"] if e["source"] == focus else (e["source"] if e["target"] == focus else None)
            if other is None:
                continue
            n = node_by_id.get(other)
            num = e.get("value_num")
            if n is None or n.get("kind") != "address" or n.get("parent") or not num:
                continue
            key = (e.get("asset_symbol") or "?", round(float(num), 8))
            buckets[key].append(other)
            bucket_label[key] = e.get("value_label") or f"{num}"
        for key, members in buckets.items():
            uniq = sorted(set(members))
            if len(uniq) < MIN_DENOM_POOL:
                continue
            gid = f"dgrp:{focus}:{key[0]}:{key[1]}"
            out_nodes.append({"id": gid, "kind": "group", "group_type": "denomination",
                              "label": f"{bucket_label[key]} ×{len(uniq)}", "denomination": bucket_label[key],
                              "pool_size": len(uniq)})
            for a in uniq:
                node_by_id[a]["parent"] = gid
            denom_groups += 1

    # P8.8 — LEIDEN COMMUNITY DETECTION (Traag 2019): VISUAL STRUCTURE only, never an ownership claim, never
    # persisted (Invariants #3/#4). Computed over the visible address graph at VIEW time and rendered as a
    # DISTINCT ``group_type='community'`` box labelled "structure, not ownership". Only groups addresses that
    # don't already have a parent (co-spend/entity/denomination grouping wins — those are evidentiary).
    community_groups = 0
    community_note = None
    if community_detect:
        from .clustering import community as _community

        node_by_id = {n["id"]: n for n in out_nodes}
        comm_edges = [e for e in (list(kept_edges.values())) if e["kind"] in ("transfer", "tx_input", "tx_output")]
        cres = _community.detect_communities(out_nodes, comm_edges)
        community_note = cres.get("note")
        members_by_comm: dict[int, list[str]] = defaultdict(list)
        for nid, ci in cres.get("communities", {}).items():
            n = node_by_id.get(nid)
            if n is not None and n.get("kind") == "address" and not n.get("parent"):
                members_by_comm[ci].append(nid)
        for ci, members in members_by_comm.items():
            if len(members) < 2:
                continue
            gid = f"comm:{focus}:{ci}"
            out_nodes.append({"id": gid, "kind": "group", "group_type": "community",
                              "label": f"community {ci + 1} ({len(members)})\nstructure, not ownership",
                              "community_index": ci, "pool_size": len(members)})
            for a in members:
                node_by_id[a]["parent"] = gid
            community_groups += 1

    # P8.7.3 #3 — collapse parallel same-(source,target,asset) fact edges visible in THIS view into one
    # display rollup (count + summed value) so the canvas is legible too; trace/aggregate edges pass through.
    # A display rollup over real facts (Inv #5); the movements stay reachable via the rollup's ``underlying``.
    fact_edges = [e for e in kept_edges.values() if e["kind"] in ("transfer", "tx_input", "tx_output")]
    other_edges = [e for e in kept_edges.values() if e["kind"] not in ("transfer", "tx_input", "tx_output")]
    parallel_collapsed = 0
    if aggregate_parallel:
        before = len(fact_edges)
        fact_edges = aggregate_parallel_edges(fact_edges)
        parallel_collapsed = before - len(fact_edges)

    # Per-view VALUE-DRIVEN THICKNESS (P3.5 feature 3), now BASIS-AWARE (P8.6): re-run the shared width
    # model over the edges visible NOW in the active basis — USD over the priced visible min/max (unpriced
    # scaled by native per asset, #2), or native-per-asset when the basis is native. Run AFTER aggregation
    # so a rollup is sized by its summed value.
    visible_fact = fact_edges
    view_basis = scale_edge_widths(visible_fact, basis=basis)

    # NATIVE display mode: the native amount label (``value_label``, e.g. "100 ETH") should WIN on every
    # fact edge, so strip the USD label (which the stylesheet draws on top when present). USD mode leaves
    # both (USD wins for priced; native shows for unpriced) — the existing behavior.
    if basis == "native":
        for e in visible_fact:
            e.pop("value_usd_label", None)

    # Honesty (P8.7.1 #2): per-category counts of what was HIDDEN/folded, so a report's scope_spec can say
    # exactly "N dust folded, M unverified collapsed, K poison folded" — what's shown vs omitted.
    hidden = {"dust": 0, "user_dust": 0, "unverified": 0, "poison": 0}
    _kind_to_bucket = {"aggregate": "dust", "user_dust": "user_dust",
                       "unverified": "unverified", "poison": "poison"}
    for n in agg_nodes:
        b = _kind_to_bucket.get(n["kind"])
        if b:
            hidden[b] += n.get("count", 0)

    displayed = sum(1 for n in out_nodes if n["kind"] in _REAL_KINDS) + len(agg_nodes)
    g_out = {
        "nodes": out_nodes,
        "edges": other_edges + fact_edges + agg_edges,
        "meta": {
            "focus": focus,
            "focus_label": nodes[focus].get("label"),
            "displayed": displayed,
            "total": total,
            "bounded": bool(bounded or displayed < total),
            "aggregated": aggregated,
            "hops": hops,
            "node_cap": node_cap,
            "group_dust": group_dust,
            "value_basis": basis,                 # 'usd' | 'native' — drives labels/thickness/dust/ordering
            "denomination_groups": denom_groups,  # # of mixer-pool clusters formed (#7)
            "community_groups": community_groups,  # P8.8 Leiden communities drawn (visual structure only)
            "community_note": community_note,      # "structure, not ownership" / unavailable note
            "parallel_collapsed": parallel_collapsed,  # # of parallel fact edges folded into rollups (#3)
            # P8.7 de-noise state + case-wide signal counts (for the banner/toggles):
            "show_unverified": show_unverified,
            "fold_poison": fold_poison,
            "unverified_token_edges": sum(1 for e in g["edges"] if e.get("token_unverified")),
            "poison_suspect_edges": sum(1 for e in g["edges"] if e.get("poison_suspect")),
            "hidden": hidden,                     # per-category folded/collapsed counts (report honesty)
            # The distinct native denominations VISIBLE now (drives the per-denomination filter panel, #1):
            "denominations": sorted({e.get("asset_symbol") for e in visible_fact if e.get("asset_symbol")}),
            # The single view value-model basis (consumed by the thickness legend + the customize UI):
            "value_min_usd": view_basis.get("min_usd"),
            "value_max_usd": view_basis.get("max_usd"),
            "user_dust_usd": user_dust_usd,
        },
    }
    return g_out
