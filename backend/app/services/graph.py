"""Graph read-model projection (phase_04 step 1).

Builds a paradigm-agnostic {nodes, edges} graph from the truthful read model so the frontend
never branches on chain:

- **EVM** (``v_value_movement`` paradigm='evm'): an address->address ``transfer`` edge.
- **Bitcoin** (paradigm='utxo'): the transaction is a VISIBLE routing node —
  input address -> ``tx`` node (from ``tx_input``) and ``tx`` node -> output address (the view's
  ``tx_output`` movement). The view never fabricates an input->output edge (Invariant #5), so the
  routing goes *through* the transaction node.

Finality (Invariant #6) is a property of TRANSACTIONS and the value MOVEMENTS within them, so it
is carried on transaction nodes and on every edge — the UI styles provisional facts distinctly.
Address nodes are evergreen (an address is an identifier, not a fact with finality), so they carry
no finality_status.

Value movements are read from the VIEW; ``tx_input`` is read for the BTC input edges (the view
only carries the output side). Addresses/transactions are batch-loaded (no per-node N+1 lookup).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from ..theme import dimension
from .entities import build_merge_resolver

# Edge width is scaled (log) by value between these bounds so dominant flows pop without dwarfing the
# rest. The bounds + the unpriced baseline are CATALOG SIZING TOKENS (overridable; the P6 customize UI
# will expose them alongside the color overrides) — read fresh each call so an override takes effect.
_EW_MIN_DEFAULT = 1.8
_EW_MAX_DEFAULT = 7.0
_EW_UNPRICED_DEFAULT = 1.5

# Known-token allowlist (P8.7 #2): an ERC-20 with a real DeFiLlama price is "verified" automatically; this
# small symbol allowlist keeps a handful of major tokens prominent even on a run where the price API
# missed them. A spam/airdrop token is unpriced AND not here -> de-emphasised (NOT a malice claim). Symbols
# are upper-cased on compare. Address-poisoning spam fakes these symbols too, so price stays the primary
# signal; the allowlist is only a safety net for genuinely-known tokens.
TOKEN_ALLOWLIST = {
    "USDC", "USDT", "DAI", "WETH", "WBTC", "BUSD", "TUSD", "USDP", "FRAX", "LUSD",
    "STETH", "WSTETH", "RETH", "CBETH", "LINK", "UNI", "AAVE", "COMP", "MKR", "CRV",
    "LDO", "SNX", "USDC.E", "MATIC", "ARB", "OP", "PYUSD",
}

# Address-poisoning heuristic (P8.7 #3): two EVM addresses are "look-alikes" if they share the same first-K
# AND last-K hex characters (the part a human eyeballs when copy-pasting). K=4 is the documented default.
_POISON_K = 4


def _ew_bounds() -> tuple[float, float, float]:
    return (dimension("edge.thickness.min", _EW_MIN_DEFAULT),
            dimension("edge.thickness.max", _EW_MAX_DEFAULT),
            dimension("edge.thickness.unpriced", _EW_UNPRICED_DEFAULT))


def scale_edge_widths(fact_edges: list[dict], *, basis: str = "usd", lo: float | None = None,
                      hi: float | None = None) -> dict:
    """Set each fact edge's ``ew`` (render width) by LOG-normalizing its value against a basis.

    The ONE width model shared by the full graph (report) and the per-view normalization (P3.5 feature
    3), now BASIS-AWARE (P8.6 USD<->native toggle):

    * ``basis="usd"`` (default): PRICED edges scale over the priced USD [lo, hi] (the cross-asset
      comparator; the visible min/max when ``build_view`` passes them, else this batch's own).
      **UNPRICED edges no longer flatten to a thin baseline** — they scale by their NATIVE amount WITHIN
      their asset (P8.6 "unpriced ≠ dust": a 100 ETH movement with no DeFiLlama price still renders
      LARGE). An unpriced edge with no native amount falls back to the neutral baseline.
    * ``basis="native"``: EVERY edge scales by native amount, PER ASSET (native isn't comparable across
      assets, so each asset normalizes over its own min/max — never one fake combined native scale).

    Returns the basis used (for honesty meta). The USD min/max are reported only for the USD basis."""
    ew_min, ew_max, ew_unpriced = _ew_bounds()

    def _log_scale(items: list[dict], key: str, blo: float, bhi: float) -> None:
        span = math.log(bhi) - math.log(blo) if bhi > blo else 0.0
        for e in items:
            v = e[key]
            tpos = 0.5 if span == 0 else (math.log(v) - math.log(blo)) / span
            tpos = min(1.0, max(0.0, tpos))  # clamp: a per-view value outside [lo,hi] pins to a bound
            e["ew"] = round(ew_min + tpos * (ew_max - ew_min), 2)

    def _scale_native_per_asset(items: list[dict]) -> None:
        """Width by native amount, normalized SEPARATELY within each asset (per-asset honesty). An edge
        with no native amount gets the neutral baseline (and never enters any asset's basis)."""
        groups: dict[str, list[dict]] = defaultdict(list)
        for e in items:
            groups[e.get("asset_symbol") or "?"].append(e)
        for grp in groups.values():
            nz = [e for e in grp if (e.get("value_num") or 0) > 0]
            if nz:
                nvals = [e["value_num"] for e in nz]
                _log_scale(nz, "value_num", min(nvals), max(nvals))
            for e in grp:
                if (e.get("value_num") or 0) <= 0:
                    e["ew"] = ew_unpriced

    if basis == "native":
        _scale_native_per_asset(fact_edges)
        return {"basis": "native", "min_usd": None, "max_usd": None}

    # USD basis: priced edges over the USD [lo,hi]; unpriced edges by native amount PER ASSET (#2 fix).
    priced = [e for e in fact_edges if (e.get("value_usd") or 0) > 0]
    if priced:
        vals = [e["value_usd"] for e in priced]
        blo = min(vals) if lo is None else lo
        bhi = max(vals) if hi is None else hi
        _log_scale(priced, "value_usd", blo, bhi)
        _scale_native_per_asset([e for e in fact_edges if (e.get("value_usd") or 0) <= 0])
        return {"basis": "usd", "min_usd": round(blo, 2), "max_usd": round(bhi, 2)}

    # No USD anywhere -> native magnitude per asset (single-asset cases / unit tests).
    _scale_native_per_asset(fact_edges)
    has_native = any((e.get("value_num") or 0) > 0 for e in fact_edges)
    return {"basis": "native" if has_native else "none", "min_usd": None, "max_usd": None}


_FACT_KINDS = ("transfer", "tx_input", "tx_output")


def aggregate_parallel_edges(edges: list[dict]) -> list[dict]:
    """Collapse PARALLEL fact edges that share ``(source, target, kind, asset)`` into ONE display edge
    carrying a ``count`` + summed value (per asset) — an honest display rollup of real same-endpoint facts,
    NOT a synthesized transfer (Invariant #5 untouched: the input→output linkage and the individual
    movements are unchanged in the DB). The collapsed movements stay reachable via the aggregate's
    ``underlying`` list (drill-down).

    A dense EVM case (e.g. Tornado: thousands of movements among a handful of nodes) is illegible when every
    movement is its own edge; collapsing same-endpoint parallels makes the exhibit readable. SINGLETONS pass
    through unchanged, and any edge carrying the investigator layer (annotation / custom label) or a per-
    movement display flag (poison-suspect / unverified-token / contested valuation) is KEPT INDIVIDUAL so its
    provenance, styling, and selectability survive — only the plain bulk parallels fold."""
    def _plain(e: dict) -> bool:
        return (e.get("kind") in _FACT_KINDS and not e.get("has_annotation") and not e.get("custom_label")
                and not e.get("poison_suspect") and not e.get("token_unverified")
                and not e.get("value_contested"))

    groups: dict[tuple, list[dict]] = defaultdict(list)
    passthrough: list[dict] = []
    for e in edges:
        if _plain(e):
            groups[(e["source"], e["target"], e["kind"], e.get("asset_symbol") or "?")].append(e)
        else:
            passthrough.append(e)

    out: list[dict] = []
    for (src, tgt, kind, asset), members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        count = len(members)
        # COR-03: sum the parallel-edge display totals in Decimal (over the members' exact values), then
        # quantize + float once, so the aggregated USD/native shown on the report never drifts sub-cent.
        total_native = float(round(sum((_dec(m.get("value_num")) for m in members), Decimal(0)), 8))
        priced = [m for m in members if m.get("value_usd") is not None]
        total_usd = (float(round(sum((_dec(m["value_usd"]) for m in priced), Decimal(0)), 2))
                     if priced else None)
        sym = None if asset == "?" else asset
        times = f" ×{count:,}"  # the ×N count baked into the value label so the canvas AND the report's
        # Python cytoscape twin both show it with no stylesheet change (the value_label/value_usd_label rules).
        base = f"{total_native:g} {sym}" if (sym and total_native) else None
        agg = {
            "id": f"par:{kind}:{src}->{tgt}:{asset}",
            "source": src, "target": tgt, "kind": kind,
            "paradigm": members[0].get("paradigm"),
            "asset_symbol": sym, "value_num": total_native,
            "value_label": (base + times) if base else times.strip(),
            # provisional if ANY member is tip (so the rollup is drawn dashed — never freeze tip as final, Inv #6)
            "finality_status": "provisional" if any(
                m.get("finality_status") == "provisional" for m in members) else "final",
            "parallel_aggregate": True, "count": count,
            "underlying": sorted(m["id"] for m in members),
        }
        # A rollup can span >1 acquiring source — keep the DISTINCT set side-by-side (Invariant #4:
        # never collapse multi-source into one). No single source_query_id on a rollup (it's many); the
        # per-movement drill-through lives on the underlying movements in the SidePanel.
        agg_sources = sorted({m.get("source_name") for m in members if m.get("source_name")})
        if agg_sources:
            agg["source_names"] = agg_sources
        if total_usd is not None:
            agg["value_usd"] = total_usd
            agg["value_usd_label"] = f"{_usd(total_usd)}{times}"
        no_price = len(members) - len(priced)
        if no_price:
            agg["no_price_count"] = no_price
        if not priced:
            agg["no_price"] = True
        out.append(agg)
    return out + passthrough


def _short(s: str | None, head: int = 8, tail: int = 6) -> str:
    if not s:
        return "?"
    return s if len(s) <= head + tail + 1 else f"{s[:head]}…{s[-tail:]}"


def _alias(s: str | None) -> str:
    """A short canvas alias for an address OR tx hash: first4…last4 (full value stays on hover + in the
    SidePanel). Applied uniformly so a tx node's label is no longer than an address alias."""
    if not s:
        return "?"
    return s if len(s) <= 9 else f"{s[:4]}…{s[-4:]}"


# Cap the entity name so the label stays exactly two lines (entity over alias) and never wraps to a third.
_ENTITY_LABEL_MAX = 22


def _cap(s: str | None, n: int = _ENTITY_LABEL_MAX) -> str | None:
    if not s:
        return s
    return s if len(s) <= n else f"{s[: n - 1]}…"


def _usd(v: float | None) -> str | None:
    """A compact USD label for an edge/summary (value-at-time). None stays None (an honest gap)."""
    if v is None:
        return None
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.4g}"  # sub-dollar: a few significant figures rather than $0.00


def _fmt_amount(amount: str | None, decimals: int | None, symbol: str | None) -> tuple[str | None, float]:
    """(value_label, value_num). Decimal-correct scaling by the asset's decimals, trailing zeros trimmed,
    symbol appended (e.g. '0.0115 BTC'). value_num is a float for width scaling only (display, not money)."""
    if amount is None or decimals is None:
        return None, 0.0
    try:
        v = Decimal(amount) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError, TypeError):
        return None, 0.0
    s = f"{v:.8f}".rstrip("0").rstrip(".") or "0"
    return (f"{s} {symbol}" if symbol else s), float(v)


def _dec(x) -> Decimal:
    """Coerce a display number to Decimal for exact summation (COR-03); None/bad → Decimal(0)."""
    if x is None:
        return Decimal(0)
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _native_dec(amount: str | None, decimals: int | None) -> Decimal:
    """The EXACT native amount as a Decimal (COR-03) — for summed display rollups that must not accumulate
    float sub-unit drift. Returns Decimal(0) on bad input (mirrors _fmt_amount's guard)."""
    if amount is None or decimals is None:
        return Decimal(0)
    try:
        return Decimal(amount) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _seed_address_id(conn) -> str | None:
    """The seed/anchor: the address the investigation started from = the address of the EARLIEST
    address-scoped acquisition (get_transactions/get_balance). Best-effort; None if undeterminable."""
    for r in conn.execute(
        "SELECT params FROM source_query WHERE capability IN ('get_transactions','get_balance') "
        "ORDER BY requested_at, id").fetchall():
        try:
            params = json.loads(r["params"]) or {} if r["params"] else {}
        except (TypeError, ValueError):
            params = {}
        addr = params.get("address")
        if not addr:
            continue
        # EFF-02: constrain chain so ux_address(chain, address) is used as an indexed seek (a bare
        # `WHERE address=?` full-SCANs the address table). The chain rides in the same params.
        chain = params.get("chain")
        if chain:
            row = conn.execute("SELECT id FROM address WHERE chain=? AND address=?", (chain, addr)).fetchone()
        else:
            row = conn.execute("SELECT id FROM address WHERE address=?", (addr,)).fetchone()
        if row:
            return row["id"]
    return None


def _node_summaries(conn) -> dict:
    """Per-address SUMMARY flags that drive on-canvas styling (the SidePanel fetches the full detail).

    Returns dicts keyed by address_id: `risk` ('sanctioned' | 'elevated'), `attributed` (set),
    `coinjoin` (set), `entity_label` (a sourced entity name for the node), plus `groups` — resolved
    entity_id -> {member address_ids, name, origin} — so the caller can draw co-spend clusters /
    sourced entities as compound parent nodes.
    """
    # risk LEVEL for styling + the distinct SOURCE(s) behind it (UX-03 hover cue: "risk: ofac-sdn"). The
    # sources are kept side-by-side (Invariant #4) — the hover names every asserting source, never one.
    risk: dict[str, str] = {}
    risk_sources: dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT address_id, category, source FROM risk_assessment").fetchall():
        if r["category"] == "sanctioned":
            risk[r["address_id"]] = "sanctioned"          # sanctioned is the strongest signal — it wins
        elif risk.get(r["address_id"]) != "sanctioned":
            risk[r["address_id"]] = "elevated"
        if r["source"]:
            risk_sources[r["address_id"]].add(r["source"])
    # Attribution presence + the distinct source(s) behind it (UX-03 hover cue), collected in one pass.
    attributed: set[str] = set()
    attribution_sources: dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT address_id, source FROM attribution").fetchall():
        attributed.add(r["address_id"])
        if r["source"]:
            attribution_sources[r["address_id"]].add(r["source"])

    # Investigator ANNOTATIONS (free-text notes; migration 0004) — a target with >=1 annotation gets a
    # green outline on the canvas. Distinct from attribution (a sourced claim). Keyed per object type.
    annotated_addr = {r["target_id"] for r in conn.execute(
        "SELECT DISTINCT target_id FROM annotation WHERE target_type='address'").fetchall()}
    annotated_tx = {r["target_id"] for r in conn.execute(
        "SELECT DISTINCT target_id FROM annotation WHERE target_type='transaction'").fetchall()}

    # Investigator display-label overrides on addresses (migration 0008) and TRANSACTIONS (migration
    # 0009). The CURRENT label is the most-recent row per target; it takes DISPLAY PRECEDENCE over the
    # auto alias / entity name below.
    custom_label: dict[str, str] = {}
    for r in conn.execute(
        "SELECT target_id, label FROM investigator_label WHERE target_type='address' "
        "ORDER BY created_at, rowid").fetchall():
        custom_label[r["target_id"]] = r["label"]   # ascending (time, then insertion) -> latest row wins
    custom_label_tx: dict[str, str] = {}
    for r in conn.execute(
        "SELECT target_id, label FROM investigator_label WHERE target_type='transaction' "
        "ORDER BY created_at, rowid").fetchall():
        custom_label_tx[r["target_id"]] = r["label"]

    coinjoin: set[str] = set()
    entity_label: dict[str, str] = {}
    groups: dict[str, dict] = {}
    resolve_terminal = build_merge_resolver(conn)  # EFF-03: batch-load the merge forest once (no N+1)
    for m in conn.execute(
        "SELECT m.address_id, m.entity_id, m.flags, e.name, e.origin "
        "FROM entity_membership m JOIN entity e ON e.id=m.entity_id").fetchall():
        rid = resolve_terminal(m["entity_id"])          # follow merged_into to the canonical entity
        g = groups.setdefault(rid, {"members": set(), "name": None, "origin": m["origin"]})
        g["members"].add(m["address_id"])
        if m["name"] and not g["name"]:
            g["name"] = m["name"]
        if m["name"] and m["address_id"] not in entity_label:
            entity_label[m["address_id"]] = m["name"]
        if m["flags"] and "possible-coinjoin" in m["flags"]:
            coinjoin.add(m["address_id"])
    return {"risk": risk, "risk_sources": risk_sources, "attributed": attributed,
            "attribution_sources": attribution_sources, "coinjoin": coinjoin,
            "entity_label": entity_label, "groups": groups, "custom_label": custom_label,
            "custom_label_tx": custom_label_tx,
            "annotated_addr": annotated_addr, "annotated_tx": annotated_tx}


def _flag_address_poisoning(nodes: dict, edges: list) -> int:
    """Flag a likely ADDRESS-POISONING attack (P8.7 #3) — a HEURISTIC, never a fact (Inv #4), recomputed
    each render (reversible, never persisted). The attack: a scammer mints a vanity address whose first-K
    + last-K hex matches a REAL counterparty of the victim, then sends the victim a ZERO-value transfer so
    the look-alike lands in their history (hoping a later copy-paste). We flag a zero-value transfer whose
    look-alike endpoint (no genuine activity) shares the first-K+last-K hex of an address the OTHER
    endpoint actually transacts with. Sets ``poison_suspect`` (+ confidence + the mimicked address) on the
    edge and the look-alike node. Returns the count flagged."""
    addr_of = {nid: n["address"] for nid, n in nodes.items()
               if n.get("kind") == "address" and n.get("address")}
    nid_of = {a: nid for nid, a in addr_of.items()}

    def pkey(a: str | None) -> tuple[str, str] | None:
        a = (a or "").lower()
        if not a.startswith("0x") or len(a) < 2 + 2 * _POISON_K:
            return None
        return (a[2:2 + _POISON_K], a[-_POISON_K:])

    transfers = [e for e in edges if e.get("kind") == "transfer"]
    max_native: dict[str, float] = defaultdict(float)
    real_partners: dict[str, set] = defaultdict(set)
    for e in transfers:
        sa, ta = addr_of.get(e["source"]), addr_of.get(e["target"])
        v = e.get("value_num") or 0.0
        if sa:
            max_native[sa] = max(max_native[sa], v)
        if ta:
            max_native[ta] = max(max_native[ta], v)
        if v > 0 and sa and ta:
            real_partners[sa].add(ta)
            real_partners[ta].add(sa)

    by_key: dict[tuple, list[str]] = defaultdict(list)
    for a in addr_of.values():
        k = pkey(a)
        if k is not None:
            by_key[k].append(a)

    flagged = 0
    for e in transfers:
        if (e.get("value_num") or 0.0) != 0.0:
            continue  # the poisoning hallmark is a ZERO-value transfer
        sa, ta = addr_of.get(e["source"]), addr_of.get(e["target"])
        if not sa or not ta:
            continue
        for look, victim in ((sa, ta), (ta, sa)):
            if max_native.get(look, 0.0) > 0:
                continue  # the look-alike must have NO genuine activity (a throwaway vanity address)
            k = pkey(look)
            if k is None:
                continue
            mimicked = next((r for r in by_key.get(k, [])
                             if r != look and r in real_partners.get(victim, set())), None)
            if mimicked is None:
                continue
            e["poison_suspect"] = True
            e["poison_confidence"] = 0.7
            e["poison_lookalike"] = mimicked       # the real address it mimics (first-K + last-K match)
            ln = nodes.get(nid_of.get(look))
            if ln is not None:
                ln["poison_suspect"] = True
                ln["poison_confidence"] = 0.7
            flagged += 1
            break
    return flagged


def build_graph(conn, *, aggregate: bool = True, focus_incident: str | None = None) -> dict:
    """Build the paradigm-agnostic ``{nodes, edges}`` read model.

    ``focus_incident`` (EFF-01): a node id (``addr:<id>`` / ``tx:<id>``). When set, the two O(case) scans
    (``v_value_movement`` + ``tx_input``) are bounded to the rows INCIDENT to that node — so the node's
    neighborhood (it + its counterparties + their edges) is built with the SAME node/edge/label/flag/
    aggregation logic, but without materializing the whole case. Used by the per-click node summary."""
    # Resolve the incidence filter once — the movement/input scans below constrain to these rows.
    _mv_where, _mv_params, _in_where, _in_params = "", (), "", ()
    if focus_incident:
        kind, _, ident = focus_incident.partition(":")
        if kind == "addr":
            _mv_where = " WHERE m.src_address_id=? OR m.dst_address_id=?"
            _mv_params = (ident, ident)
            _in_where = " WHERE i.address_id=?"
            _in_params = (ident,)
        elif kind == "tx":
            _mv_where = " WHERE m.transaction_id=?"
            _mv_params = (ident,)
            _in_where = " WHERE i.transaction_id=?"
            _in_params = (ident,)

    # Batch-load once; FK + the no-dangling-fk audit guarantee referenced rows exist.
    addr_rows = {r["id"]: r for r in conn.execute(
        "SELECT id, chain, address, address_display FROM address").fetchall()}
    tx_rows = {r["id"]: r for r in conn.execute(
        "SELECT id, chain, tx_hash, finality_status, block_height FROM transaction_").fetchall()}
    # block_height is the SEQUENCE key (P3.5 feature 1, "order by sequence"). NULL = unconfirmed/mempool
    # -> an explicit missing flag (those neighbors aren't ordered; they go to the tray).
    height_by_tx = {tid: r["block_height"] for tid, r in tx_rows.items()}

    def _seq_fields(tx_id: str | None) -> dict:
        h = height_by_tx.get(tx_id) if tx_id is not None else None
        return {"seq": int(h)} if h is not None else {"seq_missing": True}
    summ = _node_summaries(conn)
    seed_id = _seed_address_id(conn)
    # Asset lookups for per-chain amount formatting: by asset_id (view edges) + native per chain (BTC inputs).
    asset_by_id = {r["id"]: (r["symbol"], r["decimals"]) for r in conn.execute(
        "SELECT id, symbol, decimals FROM asset").fetchall()}
    native_by_chain = {r["chain"]: (r["symbol"], r["decimals"]) for r in conn.execute(
        "SELECT chain, symbol, decimals FROM asset WHERE contract_address IS NULL").fetchall()}
    native_asset_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM asset WHERE contract_address IS NULL").fetchall()}

    # Provenance source per fact row (UX-03 / FN-01 acceptance #4): the connector that acquired the row +
    # its source_query id, so the canvas can NAME the source on hover and drill through to the exact query
    # (P1's /api/source_query/{id}). Batch-loaded maps (movement_id -> (connector, source_query_id)),
    # joined to the small source_query table (one row per acquisition) — same batch-load pattern as above,
    # no per-edge N+1. tx_input sources are read inline in its own query below.
    transfer_src = {r["id"]: (r["connector"], r["source_query_id"]) for r in conn.execute(
        "SELECT tr.id, tr.source_query_id, sq.connector FROM transfer tr "
        "LEFT JOIN source_query sq ON sq.id = tr.source_query_id").fetchall()}
    txout_src = {r["id"]: (r["connector"], r["source_query_id"]) for r in conn.execute(
        "SELECT o.id, o.source_query_id, sq.connector FROM tx_output o "
        "LEFT JOIN source_query sq ON sq.id = o.source_query_id").fetchall()}

    # USD value-at-time per movement (DeFiLlama valuation claims; subject_id == movement_id). Keep ONE
    # representative figure per movement (highest confidence) for the DISPLAY value + width; a movement
    # valued by >1 source is flagged `value_contested` (the claims themselves stay side-by-side in the DB —
    # this is a display number over real claims, never a collapse, Inv #4). No valuation => no USD (gap).
    # COR-03: keep the display USD as Decimal (the stored `valuation.value` is exact Decimal TEXT) so the
    # per-node + aggregate rollups summed below preserve exactness into the court-facing report, rather
    # than accumulating float sub-cent drift. Converted to float only at JSON output (quantized).
    usd_by_mv: dict[str, Decimal] = {}
    _vconf: dict[str, float] = {}
    usd_count: dict[str, int] = {}
    for r in conn.execute("SELECT subject_id, value, confidence FROM valuation").fetchall():
        sid = r["subject_id"]
        try:
            v = Decimal(r["value"])
        except (InvalidOperation, TypeError):
            continue
        conf = r["confidence"] if r["confidence"] is not None else 0.0
        usd_count[sid] = usd_count.get(sid, 0) + 1
        if sid not in usd_by_mv or conf > _vconf[sid] or (conf == _vconf[sid] and v > usd_by_mv[sid]):
            usd_by_mv[sid] = v
            _vconf[sid] = conf
    contested = {sid for sid, n in usd_count.items() if n > 1}

    # Investigator labels + annotations on the value MOVEMENTS themselves (the flow edges) — migration
    # 0009 (rename) / 0004 (annotate). A transfer (EVM) or a tx_output (BTC) can be renamed + annotated.
    # ``movement_id`` IS the durable transfer.id / tx_output.id in v_value_movement, so these key straight
    # off ``mid`` below. Display-only over the immutable facts (Invariants #5/#6).
    custom_transfer = {r["target_id"]: r["label"] for r in conn.execute(
        "SELECT target_id, label FROM investigator_label WHERE target_type='transfer' "
        "ORDER BY created_at, rowid").fetchall()}
    custom_txout = {r["target_id"]: r["label"] for r in conn.execute(
        "SELECT target_id, label FROM investigator_label WHERE target_type='tx_output' "
        "ORDER BY created_at, rowid").fetchall()}
    annotated_transfer = {r["target_id"] for r in conn.execute(
        "SELECT DISTINCT target_id FROM annotation WHERE target_type='transfer'").fetchall()}
    annotated_txout = {r["target_id"] for r in conn.execute(
        "SELECT DISTINCT target_id FROM annotation WHERE target_type='tx_output'").fetchall()}

    # Per-node value summary accumulators (received / sent): USD-at-time across all valued assets, plus the
    # chain-native amount (the "X BTC / Y ETH" figure). Display-only aggregates over the real movements.
    in_usd: dict[str, Decimal] = defaultdict(Decimal)   # COR-03: Decimal-exact rollups (float only at output)
    out_usd: dict[str, Decimal] = defaultdict(Decimal)
    in_nat: dict[str, Decimal] = defaultdict(Decimal)
    out_nat: dict[str, Decimal] = defaultdict(Decimal)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def address_node(addr_id: str) -> str:
        nid = f"addr:{addr_id}"
        if nid not in nodes:
            r = addr_rows.get(addr_id)
            if r is None:
                raise ValueError(f"address {addr_id!r} referenced but missing (FK violation?)")
            node = {"id": nid, "kind": "address", "chain": r["chain"], "address": r["address"]}
            alias = _alias(r["address_display"] or r["address"])
            # Attach the summary flags (only when truthy — keeps the payload lean and the Cytoscape
            # `[?flag]` selectors clean). The frontend styles risk/attribution/coinjoin from these.
            if addr_id in summ["risk"]:
                node["risk_level"] = summ["risk"][addr_id]
                rs = summ["risk_sources"].get(addr_id)
                if rs:
                    node["risk_sources"] = sorted(rs)   # UX-03 hover: name every asserting source (Inv #4)
            if addr_id in summ["attributed"]:
                node["has_attribution"] = True
                asrc = summ["attribution_sources"].get(addr_id)
                if asrc:
                    node["attribution_sources"] = sorted(asrc)
            if addr_id in summ["entity_label"]:
                node["entity_label"] = summ["entity_label"][addr_id]
            if addr_id in summ["coinjoin"]:
                node["coinjoin"] = True
            if addr_id in summ["annotated_addr"]:
                node["has_annotation"] = True   # investigator note(s) -> green outline
            if addr_id == seed_id:
                node["seed"] = True
            # Label composed HERE so the live graph and the report render identically (Cytoscape shows
            # data(label)). An investigator's custom label (migration 0008) WINS — it is the display name
            # the investigator chose for this node and overrides the auto entity/alias (the underlying
            # address + its facts are untouched). Otherwise: entity-FIRST when attributed (the name is what
            # matters, capped so it stays on one line), address demoted to a short alias on the 2nd line;
            # un-attributed nodes show just the alias. Kept to (at most) TWO lines — status markers do NOT
            # ride in the text: risk/seed/coinjoin are drawn ON the glyph (risk halo+ring, seed ★ corner
            # badge, coinjoin dashed ring) by the stylesheet from the flags above. Full address stays in
            # `address`; `custom_label` flags that an investigator override is in effect (for the UI).
            custom = summ["custom_label"].get(addr_id)
            if custom:
                node["label"] = _cap(custom, 28)
                node["custom_label"] = True
            else:
                entity = _cap(node.get("entity_label"))
                node["label"] = f"{entity}\n{alias}" if entity else alias
            nodes[nid] = node
        return nid

    def external_node() -> str:
        # mint/burn (EVM) or coinbase/non-standard script (BTC) — value entering/leaving the graph.
        nodes.setdefault("external", {"id": "external", "kind": "external", "label": "(external)"})
        return "external"

    def endpoint(addr_id) -> str:
        return address_node(addr_id) if addr_id is not None else external_node()

    def transaction_node(tx_id: str) -> str:
        nid = f"tx:{tx_id}"
        if nid not in nodes:
            r = tx_rows.get(tx_id)
            if r is None:
                raise ValueError(f"transaction {tx_id!r} referenced but missing (FK violation?)")
            # Same first4…last4 alias as addresses (consistency); the full hash rides in `tx_hash`. An
            # investigator's custom label (migration 0009) WINS as the display name (the tx + its facts
            # are untouched); otherwise the short hash alias.
            tnode = {"id": nid, "kind": "transaction", "chain": r["chain"], "tx_hash": r["tx_hash"],
                     "label": _alias(r["tx_hash"]), "finality_status": r["finality_status"],
                     **_seq_fields(tx_id)}  # block_height sequence key (+missing flag) for ordering
            tx_custom = summ["custom_label_tx"].get(tx_id)
            if tx_custom:
                tnode["label"] = _cap(tx_custom, 28)
                tnode["custom_label"] = True
            if tx_id in summ["annotated_tx"]:
                tnode["has_annotation"] = True   # investigator note(s) -> green outline
            nodes[nid] = tnode
        return nid

    for m in conn.execute("SELECT * FROM v_value_movement m" + _mv_where, _mv_params).fetchall():
        symbol, decimals = asset_by_id.get(m["asset_id"], (None, None))
        value_label, value_num = _fmt_amount(m["amount"], decimals, symbol)
        mid = m["movement_id"]
        usd = usd_by_mv.get(mid)
        # Per-node summary: USD-at-time on both sides; native amount only when this IS the chain asset.
        if usd is not None:
            if m["dst_address_id"]:
                in_usd[m["dst_address_id"]] += usd
            if m["src_address_id"]:
                out_usd[m["src_address_id"]] += usd
        if m["asset_id"] in native_asset_ids and value_num:
            nat_dec = _native_dec(m["amount"], decimals)  # COR-03: exact native for the summed rollups
            if m["dst_address_id"]:
                in_nat[m["dst_address_id"]] += nat_dec
            if m["src_address_id"]:
                out_nat[m["src_address_id"]] += nat_dec
        ev = {"id": f"mv:{mid}", "amount": m["amount"], "value_label": value_label,
              "value_num": value_num, "asset_symbol": symbol, "finality_status": m["finality_status"],
              **_seq_fields(m["transaction_id"])}  # the movement's tx block_height (ordering key)
        # The acquiring source (connector) + its source_query id — the hover cue + drill-through (UX-03).
        _src_connector, _src_sqid = (transfer_src if m["paradigm"] == "evm" else txout_src).get(
            mid, (None, None))
        if _src_connector:
            ev["source_name"] = _src_connector
        if _src_sqid:
            ev["source_query_id"] = _src_sqid
        # value_num = the native amount; asset_symbol = its unit (ETH/BTC/USDC). Together these let the
        # view rank/size/threshold a movement by NATIVE amount when it has no USD price (P8.6 "unpriced ≠
        # dust") and power the USD<->native display toggle — per-asset (native isn't cross-asset comparable).
        if usd is not None:
            ev["value_usd"] = float(round(usd, 2))  # COR-03: Decimal internally, float only at output
            ev["value_usd_label"] = _usd(usd)
            if mid in contested:
                ev["value_contested"] = True  # >1 source priced it — see the node detail, not collapsed
        else:
            ev["no_price"] = True  # honest gap: a value movement with no USD price (never shown as $0)
        # Token vs native + verified/unverified (P8.7 #2). A movement of a NON-native asset (an ERC-20) is
        # a `is_token`; it is "unverified" only when it has NO real price AND isn't on the known-token
        # allowlist — the signal the view uses to de-emphasise airdrop/poison spam. A DISPLAY heuristic,
        # NOT a malice claim (native ETH + priced/allowlisted tokens stay verified).
        is_token = m["asset_id"] is not None and m["asset_id"] not in native_asset_ids
        if is_token:
            ev["is_token"] = True
            if usd is None and (symbol or "").upper() not in TOKEN_ALLOWLIST:
                ev["token_unverified"] = True
        if m["paradigm"] == "evm":
            ev.update({"source": endpoint(m["src_address_id"]), "target": endpoint(m["dst_address_id"]),
                       "kind": "transfer", "paradigm": "evm", "ann_type": "transfer", "ann_id": mid})
            if mid in custom_transfer:
                ev["custom_label"] = custom_transfer[mid]   # investigator name -> wins the edge label
            if mid in annotated_transfer:
                ev["has_annotation"] = True                 # investigator note(s) -> green edge accent
        else:  # utxo: tx node -> output address
            ev.update({"source": transaction_node(m["transaction_id"]),
                       "target": endpoint(m["dst_address_id"]), "kind": "tx_output", "paradigm": "utxo",
                       "ann_type": "tx_output", "ann_id": mid})
            if mid in custom_txout:
                ev["custom_label"] = custom_txout[mid]
            if mid in annotated_txout:
                ev["has_annotation"] = True
        edges.append(ev)

    # Bitcoin input edges: input address -> tx node (the view only carries the output side). Inputs are
    # always native BTC (no asset_id column on tx_input) — format with the chain's native asset.
    for i in conn.execute(
        "SELECT i.id, i.transaction_id, i.address_id, i.amount, i.source_query_id, "
        "t.finality_status, t.chain, sq.connector "
        "FROM tx_input i JOIN transaction_ t ON t.id=i.transaction_id "
        "LEFT JOIN source_query sq ON sq.id=i.source_query_id" + _in_where, _in_params
    ).fetchall():
        tx = transaction_node(i["transaction_id"])
        symbol, decimals = native_by_chain.get(i["chain"], (None, None))
        value_label, value_num = _fmt_amount(i["amount"], decimals, symbol)
        if i["address_id"] and value_num:
            # an input address spends native (no USD priced on inputs). COR-03: sum exact Decimal.
            out_nat[i["address_id"]] += _native_dec(i["amount"], decimals)
        in_edge = {"id": f"in:{i['id']}", "source": endpoint(i["address_id"]), "target": tx,
                   "kind": "tx_input", "paradigm": "utxo", "amount": i["amount"],
                   "value_label": value_label, "value_num": value_num, "asset_symbol": symbol,
                   "no_price": True,
                   "finality_status": i["finality_status"], **_seq_fields(i["transaction_id"])}
        if i["connector"]:                       # the acquiring source (UX-03 hover cue) + drill-through
            in_edge["source_name"] = i["connector"]
        if i["source_query_id"]:
            in_edge["source_query_id"] = i["source_query_id"]
        edges.append(in_edge)

    # Trace overlay (Bitcoin): a `trace_btc_link` is a labeled apportionment CONVENTION (basis fifo /
    # investigator), NEVER a ledger fact. Surface it as a distinct trace edge between the source- and
    # dest-output addresses so the UI can render it unmistakably differently from facts (Inv #5 — the
    # input→output linkage lives only inside a trace, here carried as `trace`, not `kind="transfer"`).
    out_addr = {r["id"]: r["address_id"] for r in conn.execute(
        "SELECT id, address_id FROM tx_output").fetchall()}
    # Per-trace display NAME = the trace's name, overridden by the investigator's latest custom label
    # (feature 5; migration 0008). Carried on every trace edge so the UI can name the path (trace list +
    # hover) and the report's trace section uses the same display name.
    trace_name = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM trace").fetchall()}
    for r in conn.execute(
        "SELECT target_id, label FROM investigator_label WHERE target_type='trace' "
        "ORDER BY created_at, rowid").fetchall():
        trace_name[r["target_id"]] = r["label"]   # ascending (time, then insertion) -> latest wins
    # Stagger trace-edge labels so adjacent FIFO/manual labels don't stack at a hub (e.g. the anchor):
    # each trace edge gets an alternating vertical offset the stylesheet applies as text-margin-y.
    _label_dy = [-10, 12, -20, 22]
    for n, l in enumerate(conn.execute(
            "SELECT l.id, l.trace_id, l.source_output_id, l.dest_output_id, l.basis FROM trace_btc_link l "
            "WHERE NOT EXISTS (SELECT 1 FROM trace_btc_link_retraction r WHERE r.trace_btc_link_id=l.id)"
            ).fetchall()):
        edge = {"id": f"tr:{l['id']}", "source": endpoint(out_addr.get(l["source_output_id"])),
                "target": endpoint(out_addr.get(l["dest_output_id"])), "kind": "trace",
                "paradigm": "trace", "trace": l["basis"], "trace_id": l["trace_id"],
                "label_dy": _label_dy[n % len(_label_dy)]}
        name = trace_name.get(l["trace_id"])
        if name:
            edge["trace_name"] = name
        edges.append(edge)

    # Grouping: where a co-spend cluster or a sourced entity groups >=2 of the addresses ACTUALLY in
    # this graph, draw it as a compound parent box (its children get a `parent`). A node has one parent,
    # so on overlap the first (sorted) qualifying group wins — deterministic.
    assigned: set[str] = set()
    for rid in sorted(summ["groups"]):
        g = summ["groups"][rid]
        in_graph = [a for a in g["members"] if f"addr:{a}" in nodes and a not in assigned]
        if len(in_graph) < 2:
            continue
        group_type = "cospend" if g["origin"] == "cospend-cluster" else "entity"
        gid = f"grp:{rid}"
        label = g["name"] or ("co-spend cluster" if group_type == "cospend" else "entity")
        nodes[gid] = {"id": gid, "kind": "group", "group_type": group_type,
                      "label": f"{label} ({len(in_graph)})"}
        for a in in_graph:
            nodes[f"addr:{a}"]["parent"] = gid
            assigned.add(a)

    # Per-node value summary (received / sent), attached to address nodes for the value header + ranked
    # list. USD-at-time across all valued assets + the chain-native amount. Display-only over real movements.
    for nid, node in nodes.items():
        if node.get("kind") != "address":
            continue
        aid = nid[len("addr:"):]
        # COR-03: quantize the Decimal-exact rollups once, then float for JSON output.
        iv = float(round(in_usd.get(aid, Decimal(0)), 2))
        ov = float(round(out_usd.get(aid, Decimal(0)), 2))
        ina = float(round(in_nat.get(aid, Decimal(0)), 8))
        outa = float(round(out_nat.get(aid, Decimal(0)), 8))
        if iv or ov or ina or outa:
            sym = native_by_chain.get(node.get("chain"), (None, None))[0]
            node["val"] = {"in_usd": iv or None, "out_usd": ov or None,
                           "in_native": ina or None, "out_native": outa or None, "native_symbol": sym}

    # Address-poisoning heuristic (P8.7 #3) — a reversible display flag over the real facts (never a fact).
    # Runs on the INDIVIDUAL edges (it keys off per-movement zero-value transfers) BEFORE aggregation.
    _flag_address_poisoning(nodes, edges)

    # P8.7.3 #3 — collapse parallel same-(source,target,asset) fact edges into one display rollup so a dense
    # EVM case is legible (the report renders this full graph; build_view passes aggregate=False and folds at
    # its own end). A display rollup over real facts — never a synthesized transfer (Inv #5).
    if aggregate:
        edges = aggregate_parallel_edges(edges)

    # Edge width ∝ value (log-scaled) so dominant flows pop and the money is followable — over the FINAL edge
    # set (post-aggregation, so a rollup is sized by its SUMMED value). `build_view` re-runs the SAME
    # `scale_edge_widths` against the per-VIEW visible min/max for the live canvas (P3.5 feature 3).
    fact = [e for e in edges if e["kind"] in _FACT_KINDS]
    scale_edge_widths(fact)

    return {"nodes": list(nodes.values()), "edges": edges}


def bound_subgraph(graph: dict, limit: int) -> dict:
    """P25/FN-20: bound an already-built graph to at most ``limit`` PRIMARY (address/tx) nodes for a
    LEA-scale case — keep the highest-degree nodes (deterministic tiebreak by id), the edges among them,
    and any group (compound) parent that still contains a kept child; add a ``meta`` block reporting the
    bound. A read-only reshaping of the payload — it drops nodes/edges for display, never a fact.

    ``limit`` counts primary nodes; a structural group parent is re-added on top of the cap when one of its
    children survives (so a compound box is never left dangling), so ``returned_nodes`` may slightly exceed
    ``limit``. When ``limit`` already covers every primary node, the graph is returned intact (truncated
    False). Deterministic: same case + same limit → same subgraph (so a report/exhibit is reproducible)."""
    nodes, edges = graph["nodes"], graph["edges"]
    primary = [n for n in nodes if n.get("kind") != "group"]
    if limit >= len(primary):
        return {**graph, "meta": {"total_nodes": len(nodes), "returned_nodes": len(nodes),
                                  "limit": limit, "truncated": False}}
    deg: dict[str, int] = defaultdict(int)
    for e in edges:
        deg[e["source"]] += 1
        deg[e["target"]] += 1
    ranked = sorted(primary, key=lambda n: (-deg[n["id"]], n["id"]))
    keep = {n["id"] for n in ranked[:limit]}
    keep |= {n["parent"] for n in nodes if n["id"] in keep and n.get("parent")}   # keep needed group boxes
    kept_nodes = []
    for n in nodes:
        if n["id"] not in keep:
            continue
        if n.get("parent") and n["parent"] not in keep:
            n = {k: v for k, v in n.items() if k != "parent"}   # its group was dropped → detach cleanly
        kept_nodes.append(n)
    kept_edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return {"nodes": kept_nodes, "edges": kept_edges,
            "meta": {"total_nodes": len(nodes), "returned_nodes": len(kept_nodes),
                     "limit": limit, "truncated": True}}
