"""Community detection — Leiden (Traag, Waltman & van Eck, "From Louvain to Leiden: guaranteeing
well-connected communities", Scientific Reports 9:5233, 2019; arXiv:1810.08473).

VISUAL STRUCTURE ONLY — NOT an ownership claim. A community is the output of optimising a resolution-
parameter (γ) quality function over the CURRENT view's graph; different γ yield different communities. It
is a *tunable lens*, not an observed on-chain fact. So this module:

  * NEVER writes an ``entity`` / ``entity_membership`` row, never feeds entity resolution, never appears in
    the report as an attribution. Promoting an algorithmic community to an ownership assertion would
    manufacture an unprovenanced, synthesized claim — exactly what Invariants #3/#4 forbid.
  * returns ephemeral grouping labels consumed by the read-model/view layer, rendered DISTINCTLY (its own
    ``group_type='community'`` decoration) and labelled "community (structure, not ownership)".

WHY LEIDEN, NOT LOUVAIN (the paper's central result, quoted): "the Louvain algorithm may yield arbitrarily
badly connected communities. In the worst case, communities may even be disconnected." Empirically up to
25% badly connected, 16% disconnected. Leiden's refinement phase GUARANTEES connectivity: "We prove that
the Leiden algorithm yields communities that are guaranteed to be connected" (γ-connected every iteration)
— so a community box never groups addresses that aren't even mutually reachable within it. We therefore use
Leiden, not Louvain.

IMPLEMENTATION: python-igraph's native Leiden (``Graph.community_leiden``) — the maintained reference path
the paper's authors endorse (igraph/leidenalg). It is an OPTIONAL dependency (igraph is GPL-licensed, hence
import-guarded and invoked at runtime, never bundled); when absent, community detection degrades to a clear
"unavailable" note rather than a wrong (possibly-disconnected) fallback.
"""

from __future__ import annotations

from collections import defaultdict

# The minimum addresses to bother grouping (a community of 1 is just a node).
MIN_COMMUNITY_SIZE = 2
# Modularity is the default objective (parameter-light, the standard "find communities" choice); resolution
# γ scales it (1.0 = standard modularity). Higher γ -> more, smaller communities. Leiden (not Louvain)
# guarantees each returned community is internally CONNECTED regardless of γ.
_LEIDEN_RESOLUTION = 1.0


def leiden_available() -> bool:
    try:
        import igraph  # noqa: F401
        return True
    except Exception:
        return False


def detect_communities(nodes: list[dict], edges: list[dict], *, resolution: float = _LEIDEN_RESOLUTION) -> dict:
    """Run Leiden over the ADDRESS subgraph of a view (``nodes``/``edges`` are read-model dicts). Returns
    ``{available, communities: {address_node_id: community_index}, n_communities, note}`` — ephemeral; the
    caller stamps a distinct ``group_type='community'`` parent. NEVER persisted (Invariants #3/#4).

    The graph is built over ADDRESS nodes only (transaction/aggregate/group nodes are routing/view
    artefacts, not entities); an edge contributes if both endpoints are address nodes. Leiden guarantees
    each returned community is internally connected (the property Louvain lacks)."""
    if not leiden_available():
        return {"available": False, "communities": {}, "n_communities": 0,
                "note": "community detection needs python-igraph (Leiden); pip install igraph"}
    import igraph

    addr_ids = [n["id"] for n in nodes if n.get("kind") == "address"]
    if len(addr_ids) < MIN_COMMUNITY_SIZE:
        return {"available": True, "communities": {}, "n_communities": 0, "note": "too few addresses"}
    idx = {nid: i for i, nid in enumerate(addr_ids)}

    # Collapse parallel/edge directions into an undirected weighted address graph (community = co-activity
    # structure, not flow direction); weight = number of movements between the pair.
    ew: dict[tuple[int, int], int] = defaultdict(int)
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in idx and t in idx and s != t:
            a, b = sorted((idx[s], idx[t]))
            ew[(a, b)] += int(e.get("count", 1) or 1)
    if not ew:
        return {"available": True, "communities": {}, "n_communities": 0, "note": "no address-address edges"}

    g = igraph.Graph(n=len(addr_ids), edges=list(ew.keys()))
    weights = list(ew.values())
    # Modularity objective (resolution-scaled); Leiden's refinement phase guarantees connected communities
    # (the property Louvain lacks). n_iterations=-1 -> iterate to a stable partition (the asymptotic guarantees).
    part = g.community_leiden(objective_function="modularity", weights=weights, resolution=resolution,
                              n_iterations=-1)

    communities: dict[str, int] = {}
    comm_index = 0
    for members in part:
        if len(members) < MIN_COMMUNITY_SIZE:
            continue  # singletons aren't a visual community
        for m in members:
            communities[addr_ids[m]] = comm_index
        comm_index += 1
    return {"available": True, "communities": communities, "n_communities": comm_index,
            "resolution": resolution, "note": "community (structure, not ownership)"}
