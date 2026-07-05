"""Traces: named, savable input->output linkages (phase_08; docs/algorithms.md §6).

A trace is an investigator construction. EVM edges reference real ``transfer`` facts
(``trace_transfer``). Bitcoin edges are ``trace_btc_link`` rows carrying an explicit ``basis``:

- ``fifo``        — apportionment by the FIFO convention (Clayton's Case / *D'Aloia*). A reproducible
                    LABEL, **never** ground-truth flow. Applied along a path the investigator has
                    already expanded — not automated path discovery.
- ``investigator``— a manual link/override the investigator asserts.

``fifo_apportion`` is a pure function (no DB) so the conservation property is tested where it lives.
The schema's ``trace_btc_link`` stores the (source_output -> dest_output) adjacency + basis; the
apportioned amount is carried in ``note`` for transparency (the table has no amount column — the
linkage is a claim, not a measured fact).
"""

from __future__ import annotations

from collections import defaultdict, deque

from ..db import repository as repo
from ..db.repository import utc_now_iso
from ..models import (
    Trace,
    TraceBridgeLink,
    TraceBtcLink,
    TraceBtcLinkRetraction,
    TraceTransfer,
    TraceTransferRetraction,
)

FIFO_BASIS = "fifo"
INVESTIGATOR_BASIS = "investigator"


# --- pure apportionment (no DB; conservation guaranteed here) --------------------------------

def fifo_apportion(inputs: list[tuple[str, int]],
                   outputs: list[tuple[str, int]]) -> list[tuple[str, str, int]]:
    """Apportion ``inputs`` to ``outputs`` in ledger order by the FIFO convention.

    ``inputs``/``outputs`` are ``(key, amount)`` pairs already sorted by index. Returns
    ``(in_key, out_key, amount)`` links. The remainder of inputs after all outputs are filled is
    the fee (not linked). Conservation: sum into any output == its amount; sum out of any input
    <= its amount; amounts > 0; total linked == total outputs (== inputs - fee).
    """
    if any(a < 0 for _, a in inputs) or any(a < 0 for _, a in outputs):
        raise ValueError("negative amount in FIFO apportionment")
    in_queue = deque((k, int(a)) for k, a in inputs)
    out_queue = deque((k, int(a)) for k, a in outputs)
    links: list[tuple[str, str, int]] = []
    if not in_queue:
        return links
    cur_in_key, cur_in_amt = in_queue.popleft()
    while out_queue:
        out_key, out_need = out_queue.popleft()
        while out_need > 0:
            take = min(cur_in_amt, out_need)
            if take > 0:
                links.append((cur_in_key, out_key, take))
            cur_in_amt -= take
            out_need -= take
            if cur_in_amt == 0:
                if in_queue:
                    cur_in_key, cur_in_amt = in_queue.popleft()
                else:
                    return links  # inputs exhausted (outputs underfunded — shouldn't happen on real txs)
    return links


# --- trace construction ----------------------------------------------------------------------

def create_trace(conn, *, name: str, description: str | None = None, now: str | None = None) -> str:
    return repo.insert_trace(conn, Trace(name=name, description=description), now=now)


def add_trace_transfer(conn, *, trace_id: str, transfer_id: str, ordering: int | None = None,
                       note: str | None = None) -> str:
    """Add an EVM edge referencing a real ``transfer`` fact (A->B is a ledger fact)."""
    if conn.execute("SELECT 1 FROM transfer WHERE id=?", (transfer_id,)).fetchone() is None:
        raise ValueError(f"transfer {transfer_id!r} not found")
    return repo.insert_trace_transfer(conn, TraceTransfer(
        trace_id=trace_id, transfer_id=transfer_id, ordering=ordering, note=note))


def fifo_trace_transaction(conn, *, trace_id: str, transaction_id: str,
                           ordering_start: int = 0) -> dict:
    """Apportion one Bitcoin transaction by FIFO and write ``trace_btc_link(basis='fifo')`` rows.

    Each input is keyed to the prev tx_output it spends (``source_output_id``); an input whose
    prev output is not in-DB cannot be linked (the column is NOT NULL) — it is reported as
    ``unresolved`` rather than guessed.
    """
    ins = conn.execute(
        "SELECT id, prev_output_id, amount FROM tx_input WHERE transaction_id=? ORDER BY input_index",
        (transaction_id,)).fetchall()
    outs = conn.execute(
        "SELECT id, amount FROM tx_output WHERE transaction_id=? ORDER BY output_index",
        (transaction_id,)).fetchall()

    prev_of = {r["id"]: r["prev_output_id"] for r in ins}
    links = fifo_apportion([(r["id"], int(r["amount"])) for r in ins],
                           [(r["id"], int(r["amount"])) for r in outs])

    # Defensive: the apportionment must respect conservation before anything is stored — never
    # overfund an output or overspend an input (a guard against algorithm drift / bad data).
    in_amt = {r["id"]: int(r["amount"]) for r in ins}
    out_amt = {r["id"]: int(r["amount"]) for r in outs}
    into, outof = defaultdict(int), defaultdict(int)
    for in_id, out_id, amount in links:
        if amount <= 0:
            raise ValueError("FIFO produced a non-positive link amount")
        into[out_id] += amount
        outof[in_id] += amount
    for out_id, got in into.items():
        if got > out_amt[out_id]:
            raise ValueError(f"FIFO overfunds output {out_id!r} ({got} > {out_amt[out_id]})")
    for in_id, got in outof.items():
        if got > in_amt[in_id]:
            raise ValueError(f"FIFO overspends input {in_id!r} ({got} > {in_amt[in_id]})")

    written, unresolved = 0, 0
    ordering = ordering_start
    for in_id, out_id, amount in links:
        src_output = prev_of.get(in_id)
        if src_output is None:
            unresolved += 1  # input spends an output not in-DB — cannot anchor the link
            continue          # ordering stays dense (no row is written for an unresolved link)
        repo.insert_trace_btc_link(conn, TraceBtcLink(
            trace_id=trace_id, transaction_id=transaction_id,
            source_output_id=src_output, dest_output_id=out_id, basis=FIFO_BASIS,
            confidence=None, ordering=ordering, note=f"fifo apportioned {amount} sat"))
        written += 1
        ordering += 1
    return {"links_written": written, "unresolved": unresolved, "next_ordering": ordering}


def add_manual_link(conn, *, trace_id: str, transaction_id: str, source_output_id: str,
                    dest_output_id: str, confidence: float | None = None,
                    ordering: int | None = None, note: str | None = None) -> str:
    """Investigator-asserted Bitcoin link (``basis='investigator'``). Never overwrites a fact.

    A link is an apportionment *within one transaction*: ``dest_output_id`` must be an output OF
    ``transaction_id`` and ``source_output_id`` must be a prev-output an input of that transaction
    actually spends. Enforcing this keeps a manual override coherent with Invariant #5 (the link is
    a within-tx claim, never a cross-tx fabricated edge).
    """
    if conn.execute("SELECT 1 FROM transaction_ WHERE id=?", (transaction_id,)).fetchone() is None:
        raise ValueError(f"transaction {transaction_id!r} not found")
    if conn.execute("SELECT 1 FROM tx_output WHERE id=? AND transaction_id=?",
                    (dest_output_id, transaction_id)).fetchone() is None:
        raise ValueError(f"dest_output_id {dest_output_id!r} must be an output of {transaction_id!r}")
    if conn.execute("SELECT 1 FROM tx_input WHERE transaction_id=? AND prev_output_id=?",
                    (transaction_id, source_output_id)).fetchone() is None:
        raise ValueError(
            f"source_output_id {source_output_id!r} must be spent by an input of {transaction_id!r}")
    return repo.insert_trace_btc_link(conn, TraceBtcLink(
        trace_id=trace_id, transaction_id=transaction_id, source_output_id=source_output_id,
        dest_output_id=dest_output_id, basis=INVESTIGATOR_BASIS, confidence=confidence,
        ordering=ordering, note=note))


def trace_btc_links(conn, trace_id: str) -> list[dict]:
    """Render a trace's Bitcoin links, each labeled with its basis (a convention, not flow). FN-04:
    RETRACTED links are excluded (the row persists; it is just no longer part of the effective trace)."""
    rows = conn.execute(
        "SELECT l.id, l.transaction_id, l.source_output_id, l.dest_output_id, l.basis, l.confidence, "
        "l.ordering, l.note FROM trace_btc_link l WHERE l.trace_id=? "
        "AND NOT EXISTS (SELECT 1 FROM trace_btc_link_retraction r WHERE r.trace_btc_link_id=l.id) "
        "ORDER BY l.ordering, l.id", (trace_id,)).fetchall()
    return [{**dict(r), "is_convention": r["basis"] == FIFO_BASIS} for r in rows]


def trace_transfers(conn, trace_id: str) -> list[dict]:
    """A trace's EVM edges (each a real ``transfer`` fact), EXCLUDING retracted ones (FN-04). Ordered as
    the report renders them. The edge row persists after a retract — it is just no longer effective."""
    rows = conn.execute(
        "SELECT tt.transfer_id, tt.ordering, tt.note FROM trace_transfer tt WHERE tt.trace_id=? "
        "AND NOT EXISTS (SELECT 1 FROM trace_transfer_retraction r WHERE r.trace_transfer_id=tt.id) "
        "ORDER BY tt.ordering, tt.id", (trace_id,)).fetchall()
    return [dict(r) for r in rows]


# --- guided expansion (FN-16): PROPOSE next hops from the frontier; NEVER auto-add -------------

def trace_next_hops(conn, trace_id: str, *, limit: int = 200) -> dict:
    """PROPOSE candidate next hops from a trace's TERMINAL nodes, drawn ONLY from facts already in the case.

    A terminal is a **leaf of the effective (non-retracted) trace**: an EVM address value ARRIVED at but
    hasn't been traced onward (a `to` of some edge, never a `from`), or a BTC output that is a link
    destination but not yet a link source. From each terminal this surfaces the outgoing facts the
    investigator *could* add next.

    This is strictly **READ-ONLY** — it adds nothing. The human picks which candidate to add (EVM via the
    add-transfer endpoint, BTC via a within-tx link); the tool never auto-decides a path or attributes flow
    (Invariants #4/#5; the scope guard against becoming auto-discovery). Only ONE hop out from the current
    frontier is proposed at a time — advancing requires an explicit user pick, which moves the frontier.

    Returns ``{"evm": [...], "btc": [...]}``:
      - ``evm``: in-DB ``transfer`` facts leaving a terminal address, excluding edges already in the trace.
      - ``btc``: a terminal output that a known (in-DB) transaction spends — the transaction to extend
        through (the specific destination output is chosen when the link is actually added).
    """
    # EVM frontier: `to` addresses that are not also a `from` in the effective trace.
    evm_edges = conn.execute(
        "SELECT tr.from_address_id AS f, tr.to_address_id AS t, tt.transfer_id AS tid "
        "FROM trace_transfer tt JOIN transfer tr ON tr.id=tt.transfer_id "
        "WHERE tt.trace_id=? AND NOT EXISTS "
        "(SELECT 1 FROM trace_transfer_retraction r WHERE r.trace_transfer_id=tt.id)", (trace_id,)).fetchall()
    froms = {e["f"] for e in evm_edges if e["f"] is not None}
    in_trace = {e["tid"] for e in evm_edges}
    evm_terminals = {e["t"] for e in evm_edges if e["t"] is not None} - froms

    evm: list[dict] = []
    for addr_id in sorted(evm_terminals):
        for r in conn.execute(
            "SELECT tr.id, tr.chain, tr.amount, fa.address_display AS from_disp, "
            "  ta.address_display AS to_disp, a.symbol AS asset_symbol "
            "FROM transfer tr LEFT JOIN address fa ON fa.id=tr.from_address_id "
            "  LEFT JOIN address ta ON ta.id=tr.to_address_id JOIN asset a ON a.id=tr.asset_id "
            "WHERE tr.from_address_id=? ORDER BY tr.id LIMIT ?", (addr_id, limit)).fetchall():
            if r["id"] in in_trace:
                continue  # already an edge — not a NEW hop
            evm.append({"kind": "evm", "transfer_id": r["id"], "chain": r["chain"],
                        "from": r["from_disp"], "to": r["to_disp"],
                        "asset": r["asset_symbol"], "amount": r["amount"]})

    # BTC frontier: dest outputs that are not also a source in the effective trace.
    btc_links = conn.execute(
        "SELECT l.source_output_id AS s, l.dest_output_id AS d FROM trace_btc_link l WHERE l.trace_id=? "
        "AND NOT EXISTS (SELECT 1 FROM trace_btc_link_retraction r WHERE r.trace_btc_link_id=l.id)",
        (trace_id,)).fetchall()
    sources = {l["s"] for l in btc_links}
    btc_terminals = {l["d"] for l in btc_links} - sources

    btc: list[dict] = []
    for out_id in sorted(btc_terminals):
        r = conn.execute(
            "SELECT o.spent, o.spending_tx_id, o.output_index, o.amount, a.address_display AS addr, "
            "  x.tx_hash AS spend_hash FROM tx_output o LEFT JOIN address a ON a.id=o.address_id "
            "  LEFT JOIN transaction_ x ON x.id=o.spending_tx_id WHERE o.id=?", (out_id,)).fetchone()
        if r and r["spent"] and r["spending_tx_id"]:  # a known in-DB tx spends this terminal output
            btc.append({"kind": "btc", "source_output_id": out_id,
                        "source_label": f"out #{r['output_index']} · {r['amount']} sat · {r['addr'] or '?'}",
                        "spending_tx_id": r["spending_tx_id"], "tx_hash": r["spend_hash"]})

    return {"evm": evm, "btc": btc}


# --- cross-chain bridge link (FN-17): a manual investigator CLAIM, never a fact ----------------

def _movement_chain(conn, subject_type: str, subject_id: str) -> str | None:
    """The chain of a value movement (``transfer`` | ``tx_output``), or ``None`` if it isn't in the case."""
    if subject_type == "transfer":
        r = conn.execute("SELECT chain FROM transfer WHERE id=?", (subject_id,)).fetchone()
    else:  # tx_output — chain lives on its transaction
        r = conn.execute("SELECT tx.chain FROM tx_output o JOIN transaction_ tx ON tx.id=o.transaction_id "
                         "WHERE o.id=?", (subject_id,)).fetchone()
    return r["chain"] if r else None


def add_bridge_link(conn, *, trace_id: str, src_subject_type: str, src_subject_id: str,
                    dst_subject_type: str, dst_subject_id: str, note: str | None = None,
                    confidence: float | None = None, ordering: int | None = None,
                    now: str | None = None) -> str:
    """Assert a manual CROSS-CHAIN bridge crossing inside a trace (FN-17): value that LEFT via a movement on
    chain A corresponds to value that ARRIVED via a movement on chain B. Recorded as a
    ``basis='investigator'`` link in ``trace_bridge_link`` — a labeled CLAIM, never a synthesized
    ``transfer``/ledger fact (Invariant #5) and never a collapse of the two sides (Invariant #4). Both
    movements must exist and be on DIFFERENT chains (a same-chain link is not a bridge). Manual assertion
    only — there is no automated bridge detection (RJ-02). Returns the link id."""
    if conn.execute("SELECT 1 FROM trace WHERE id=?", (trace_id,)).fetchone() is None:
        raise ValueError(f"trace {trace_id!r} not found")
    src_chain = _movement_chain(conn, src_subject_type, src_subject_id)
    if src_chain is None:
        raise ValueError(f"source {src_subject_type} {src_subject_id!r} not found")
    dst_chain = _movement_chain(conn, dst_subject_type, dst_subject_id)
    if dst_chain is None:
        raise ValueError(f"dest {dst_subject_type} {dst_subject_id!r} not found")
    if src_chain == dst_chain:
        raise ValueError(f"a bridge link must cross chains, but both movements are on {src_chain!r}")
    return repo.insert_trace_bridge_link(conn, TraceBridgeLink(
        trace_id=trace_id, src_subject_type=src_subject_type, src_subject_id=src_subject_id,
        dst_subject_type=dst_subject_type, dst_subject_id=dst_subject_id, note=note,
        confidence=confidence, ordering=ordering), now=now)


def trace_bridge_links(conn, trace_id: str) -> list[dict]:
    """A trace's cross-chain bridge links, each enriched with the two movements' chains for display. A
    labeled investigator claim (``basis='investigator'``), shown side-by-side in panel + report."""
    rows = conn.execute(
        "SELECT id, src_subject_type, src_subject_id, dst_subject_type, dst_subject_id, basis, confidence, "
        "ordering, note FROM trace_bridge_link WHERE trace_id=? ORDER BY ordering, id", (trace_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["src_chain"] = _movement_chain(conn, r["src_subject_type"], r["src_subject_id"])
        d["dst_chain"] = _movement_chain(conn, r["dst_subject_type"], r["dst_subject_id"])
        out.append(d)
    return out


# --- retraction (FN-04): withdraw a specific edge/link, append-only — never delete --------------

def retract_trace_transfer(conn, *, trace_transfer_id: str, reason: str, now: str | None = None) -> str:
    """Retract an EVM trace edge: append a retraction row so the edge drops out of the effective trace +
    report, WITHOUT deleting the edge (append-only investigator history). Idempotent — a second retract of
    the same edge returns the existing retraction id (no duplicate). Returns the retraction id."""
    if conn.execute("SELECT 1 FROM trace_transfer WHERE id=?", (trace_transfer_id,)).fetchone() is None:
        raise ValueError(f"trace_transfer {trace_transfer_id!r} not found")
    existing = conn.execute(
        "SELECT id FROM trace_transfer_retraction WHERE trace_transfer_id=?", (trace_transfer_id,)).fetchone()
    if existing is not None:
        return existing[0]
    return repo.insert_trace_transfer_retraction(
        conn, TraceTransferRetraction(trace_transfer_id=trace_transfer_id, reason=reason), now=now)


def retract_trace_btc_link(conn, *, trace_btc_link_id: str, reason: str, now: str | None = None) -> str:
    """Retract a Bitcoin trace link (mirrors :func:`retract_trace_transfer`). Append-only, idempotent."""
    if conn.execute("SELECT 1 FROM trace_btc_link WHERE id=?", (trace_btc_link_id,)).fetchone() is None:
        raise ValueError(f"trace_btc_link {trace_btc_link_id!r} not found")
    existing = conn.execute(
        "SELECT id FROM trace_btc_link_retraction WHERE trace_btc_link_id=?", (trace_btc_link_id,)).fetchone()
    if existing is not None:
        return existing[0]
    return repo.insert_trace_btc_link_retraction(
        conn, TraceBtcLinkRetraction(trace_btc_link_id=trace_btc_link_id, reason=reason), now=now)
