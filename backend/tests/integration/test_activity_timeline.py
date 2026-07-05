"""Case activity timeline (P24/FN-14): one read-only, time-ordered log of every case event.

Builds a case with events of EVERY kind — a data fetch, a trace, a finding, an annotation, a tag, a trace
retraction, a cross-chain bridge link, an exhibit, and a report — inserted OUT of chronological order, then
asserts the timeline is a single stream ordered by time, covers all kinds, is deterministic on equal
timestamps, and writes nothing. Audits stay green (every FK chain is valid).
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.activity import case_activity
from backend.tests.integration._helpers import new_case

FRM, TO, TO2 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40
TXA, TXB = "0x" + "a" * 64, "0x" + "b" * 64
ETH = Asset(chain="ethereum", contract_address=None, symbol="ETH", decimals=18)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Activity")
    yield conn, db
    conn.close()


def _seed_two_transfers(conn):
    """Ingest two ethereum transfers under ONE source_query (the fetch event, requested_at 2026-01-01).
    Returns (transfer_id_1, transfer_id_2) for the trace/bridge FK chains."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": FRM, "bounds": "default"}, requested_at="2026-01-01T00:00:00Z",
                     status="ok")

    def w(c, sqid):
        aid = repo.upsert_asset(c, ETH, sqid)
        fid = repo.upsert_address(c, Address(chain="ethereum", address_display=FRM), sqid)
        for txh, to, amount in ((TXA, TO, "1000000000000000000"), (TXB, TO2, "2000000000000000000")):
            txid = repo.upsert_transaction(c, Transaction(
                chain="ethereum", tx_hash=txh, block_height=1, finality_status="provisional"), sqid)
            tid = repo.upsert_address(c, Address(chain="ethereum", address_display=to), sqid)
            repo.upsert_transfer(c, Transfer(
                transaction_id=txid, chain="ethereum", from_address_id=fid, to_address_id=tid,
                asset_id=aid, amount=amount, transfer_type="native", position=0, occurrence=0), sqid)

    write_with_provenance(conn, sq, w)
    return [r[0] for r in conn.execute("SELECT id FROM transfer ORDER BY amount").fetchall()]


def _seed_all_event_kinds(conn):
    """Insert one row of every timestamped kind, with OUT-OF-ORDER timestamps, on valid FK chains."""
    t1, t2 = _seed_two_transfers(conn)                                # fetch @ 2026-01-01
    addr_id = conn.execute("SELECT id FROM address WHERE address=?", (FRM,)).fetchone()[0]
    ex = conn.execute
    ex("INSERT INTO trace (id, name, created_at) VALUES ('tr1','Suspect flow','2026-02-01T00:00:00Z')")
    ex("INSERT INTO exhibit (id, exhibit_type, captured_at, file_ref, content_hash) "
       "VALUES ('ex1','screenshot','2026-02-05T00:00:00Z','exhibits/a.png','h1')")
    ex("INSERT INTO finding (id, statement, created_at) VALUES ('f1','Funds reached the exchange.','2026-02-15T00:00:00Z')")
    ex("INSERT INTO annotation (id, target_type, target_id, content, created_at) "
       "VALUES ('an1','transfer',?, 'note','2026-02-20T00:00:00Z')", (t1,))
    ex("INSERT INTO tag (id, target_type, target_id, label, created_at) "
       "VALUES ('tg1','address',?, 'exchange','2026-02-25T00:00:00Z')", (addr_id,))
    # trace_edit: a trace_transfer edge + its append-only retraction.
    ex("INSERT INTO trace_transfer (id, trace_id, transfer_id, ordering) VALUES ('tt1','tr1',?,0)", (t1,))
    ex("INSERT INTO trace_transfer_retraction (id, trace_transfer_id, reason, source, created_at) "
       "VALUES ('rt1','tt1','wrong hop','investigator','2026-02-28T00:00:00Z')")
    # bridge_link: two movements as src/dst (poly refs the no-dangling-fk audit validates).
    ex("INSERT INTO trace_bridge_link (id, trace_id, src_subject_type, src_subject_id, dst_subject_type, "
       "dst_subject_id, basis, created_at) VALUES ('bl1','tr1','transfer',?, 'transfer',?, "
       "'investigator','2026-03-01T00:00:00Z')", (t1, t2))
    ex("INSERT INTO report (id, title, generated_at, scope_spec, rendered_file_ref, content_hash) "
       "VALUES ('rp1','Case report','2026-03-10T00:00:00Z','{}','reports/r.html','h2')")


def test_orders_all_event_types_by_time(case):
    conn, db = case
    _seed_all_event_kinds(conn)

    events = case_activity(conn)
    kinds = [e["kind"] for e in events]
    ts = [e["ts"] for e in events]

    # A single stream, strictly time-ordered.
    assert ts == sorted(ts)
    # Every event kind is present (fetch covers the acquisition; the rest are investigator constructions).
    assert set(kinds) == {"fetch", "trace", "exhibit", "finding", "annotation", "tag",
                          "trace_edit", "bridge_link", "report"}
    # The fetch (2026-01-01) is first, the report (2026-03-10) is last.
    assert events[0]["kind"] == "fetch" and events[0]["ts"] == "2026-01-01T00:00:00Z"
    assert events[-1]["kind"] == "report" and events[-1]["ts"] == "2026-03-10T00:00:00Z"
    # Every event carries the render contract.
    for e in events:
        assert set(e) == {"ts", "kind", "summary", "ref_type", "ref_id", "detail"} and e["summary"]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_empty_case_has_empty_timeline(case):
    conn, _ = case
    assert case_activity(conn) == []


def test_timeline_is_read_only(case):
    conn, _ = case
    _seed_all_event_kinds(conn)
    counts = lambda: tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("source_query", "trace", "finding", "annotation", "tag", "exhibit",
                                     "report", "trace_transfer_retraction", "trace_bridge_link"))
    pre = counts()
    case_activity(conn)
    case_activity(conn)
    assert counts() == pre


def test_equal_timestamps_have_deterministic_order(case):
    conn, _ = case
    # Two events at the SAME instant: the (ts, kind, ref_id) sort makes their order stable across renders.
    conn.execute("INSERT INTO finding (id, statement, created_at) VALUES ('f2','B','2026-05-01T00:00:00Z')")
    conn.execute("INSERT INTO trace (id, name, created_at) VALUES ('trA','A','2026-05-01T00:00:00Z')")
    order1 = [(e["kind"], e["ref_id"]) for e in case_activity(conn)]
    order2 = [(e["kind"], e["ref_id"]) for e in case_activity(conn)]
    assert order1 == order2                                   # reproducible
    # 'finding' sorts before 'trace' at the same ts (kind tiebreak).
    assert order1 == [("finding", "f2"), ("trace", "trA")]


def test_fetch_event_covers_enrichment(case):
    conn, _ = case
    # A DeFiLlama pricing run and an Arkham pull are each ONE source_query -> a fetch event (the granularity
    # decision: valuation/attribution acquisition appears at the fetch grain, not one event per claim row).
    conn.execute("INSERT INTO source_query (id, connector, capability, endpoint, requested_at, status) "
                 "VALUES ('sqd','defillama','get_prices','coins/prices','2026-04-01T00:00:00Z','ok')")
    conn.execute("INSERT INTO source_query (id, connector, capability, endpoint, requested_at, status) "
                 "VALUES ('sqa','arkham','get_intelligence','intelligence/address','2026-04-02T00:00:00Z','ok')")
    fetches = [e for e in case_activity(conn) if e["kind"] == "fetch"]
    conns = {e["summary"].split(" · ")[0].removeprefix("Fetched ") for e in fetches}
    assert {"defillama", "arkham"} <= conns
