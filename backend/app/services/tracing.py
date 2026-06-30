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
from ..models import Trace, TraceBtcLink, TraceTransfer

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
    """Render a trace's Bitcoin links, each labeled with its basis (a convention, not flow)."""
    rows = conn.execute(
        "SELECT id, transaction_id, source_output_id, dest_output_id, basis, confidence, ordering, note "
        "FROM trace_btc_link WHERE trace_id=? ORDER BY ordering, id", (trace_id,)).fetchall()
    return [{**dict(r), "is_convention": r["basis"] == FIFO_BASIS} for r in rows]
