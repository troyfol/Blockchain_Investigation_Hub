"""Investigator UX (P1.5): durable annotations + green outline, findings composer, report flow, and the
architecture guardrail — view operations mutate ZERO case rows (view state is ephemeral; only investigator
inputs are durable claims, never facts — Inv #3/#4/#5)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.audits.runner import run_audits
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.services.graph import build_graph
from backend.tests.integration.test_seeded_case import seed_btc_tx, seed_evm_transfer


def _seed(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Investigator UX")
    seed_evm_transfer(conn, final=True)
    seed_btc_tx(conn)
    return conn, db


def _fingerprint(db, exclude: set[str]) -> dict:
    """Full contents of every table (except ``exclude``) as {table: [row-tuples]}, opened fresh so it
    reflects committed state. Used to prove a write touched ONLY the intended table (DoD §5 guardrail)."""
    conn = get_connection(db)
    try:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        return {t: [tuple(row) for row in conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()]
                for t in tables if t not in exclude}
    finally:
        conn.close()


@pytest.fixture
def case(tmp_path):
    conn, db = _seed(tmp_path)
    ids = {
        "addr": conn.execute("SELECT id FROM address LIMIT 1").fetchone()["id"],
        "tx": conn.execute("SELECT id FROM transaction_ WHERE chain='bitcoin' LIMIT 1").fetchone()["id"],
        "transfer": conn.execute("SELECT id FROM transfer LIMIT 1").fetchone()["id"],
        "txout": conn.execute("SELECT id FROM tx_output LIMIT 1").fetchone()["id"],
    }
    yield conn, db, ids
    conn.close()


@pytest.fixture
def client(case):
    _conn, db, ids = case
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    yield TestClient(app), ids
    app.dependency_overrides.clear()


# --- B: annotations + green outline -------------------------------------------------------

def test_has_annotation_flag_on_address_and_tx(case):
    conn, _db, ids = case
    from backend.app.services.investigator import add_annotation
    add_annotation(conn, target_type="address", target_id=ids["addr"], content="seen in mixer")
    add_annotation(conn, target_type="transaction", target_id=ids["tx"], content="peel chain start")
    g = build_graph(conn)
    anode = next(n for n in g["nodes"] if n["id"] == f"addr:{ids['addr']}")
    tnode = next(n for n in g["nodes"] if n["id"] == f"tx:{ids['tx']}")
    assert anode.get("has_annotation") is True and tnode.get("has_annotation") is True
    # an un-annotated node carries no flag (clean payload + `[?flag]` selector)
    other = next(n for n in g["nodes"] if n["kind"] == "address" and n["id"] != f"addr:{ids['addr']}")
    assert "has_annotation" not in other


def test_annotation_endpoints(client):
    c, ids = client
    nid = f"addr:{ids['addr']}"
    assert c.get(f"/api/node/{nid}/annotations").json() == {"annotations": []}
    r = c.post(f"/api/node/{nid}/annotations", json={"content": "first note"})
    assert r.status_code == 200 and len(r.json()["annotations"]) == 1
    c.post(f"/api/node/{nid}/annotations", json={"content": "second note"})
    assert len(c.get(f"/api/node/{nid}/annotations").json()["annotations"]) == 2
    # green outline now shows on the node when it is in view (focus it so the bounded view includes it)
    view = c.get("/api/view", params={"focus": nid, "node_cap": 50}).json()
    assert any(n.get("has_annotation") for n in view["nodes"] if n["id"] == nid)
    assert c.post(f"/api/node/{nid}/annotations", json={"content": "  "}).status_code == 400   # empty
    assert c.post("/api/node/agg:x:in/annotations", json={"content": "x"}).status_code == 400  # not a real obj


# --- A (follow-up): relabel + annotate transactions AND flows (edges) via generic endpoints ----

def test_relabel_transaction_and_flow(client):
    c, ids = client
    # rename a TRANSACTION node -> the custom label wins the tx node's display name
    r = c.post(f"/api/target/transaction/{ids['tx']}/label", json={"label": "Peel hop #1"})
    assert r.status_code == 200
    tnode = next(n for n in r.json()["graph"]["nodes"] if n["id"] == f"tx:{ids['tx']}")
    assert tnode["label"] == "Peel hop #1" and tnode.get("custom_label") is True

    # rename an EVM TRANSFER flow -> the edge carries the custom label + its durable target (ann_*)
    r2 = c.post(f"/api/target/transfer/{ids['transfer']}/label", json={"label": "stolen ETH"})
    assert r2.status_code == 200
    e = next(x for x in r2.json()["graph"]["edges"] if x.get("ann_id") == ids["transfer"])
    assert e["custom_label"] == "stolen ETH" and e["ann_type"] == "transfer"

    # rename a BTC OUTPUT flow
    r3 = c.post(f"/api/target/tx_output/{ids['txout']}/label", json={"label": "change output"})
    assert r3.status_code == 200
    eo = next(x for x in r3.json()["graph"]["edges"] if x.get("ann_id") == ids["txout"])
    assert eo["custom_label"] == "change output" and eo["ann_type"] == "tx_output"

    # guardrails: a non-relabelable type / an empty label / an unknown id
    assert c.post(f"/api/target/asset/{ids['tx']}/label", json={"label": "x"}).status_code == 400
    assert c.post(f"/api/target/transaction/{ids['tx']}/label", json={"label": " "}).status_code == 400
    assert c.post("/api/target/transaction/ghost/label", json={"label": "x"}).status_code == 404

    # a display label NEVER touches the facts (Inv #5/#6) — the audits stay green.
    db = c.app.dependency_overrides[get_case_db_path]()
    assert all(r.passed for r in run_audits(db_path=db))


def test_annotate_flow_green_accent_and_notes(client):
    c, ids = client
    tt, tid = "transfer", ids["transfer"]
    assert c.get(f"/api/target/{tt}/{tid}/annotations").json() == {"annotations": []}
    r = c.post(f"/api/target/{tt}/{tid}/annotations", json={"content": "laundering hop"})
    assert r.status_code == 200 and len(r.json()["annotations"]) == 1

    # the read-model marks the flow edge has_annotation -> green glow on the canvas
    g = c.get("/api/graph").json()
    e = next(x for x in g["edges"] if x.get("ann_id") == tid)
    assert e.get("has_annotation") is True

    # and the notes aggregator groups the flow's note under target_type=transfer
    notes = c.get("/api/investigator/notes").json()["notes"]
    assert any(grp["target_type"] == "transfer" and grp["target_id"] == tid for grp in notes)

    assert c.post(f"/api/target/asset/{tid}/annotations", json={"content": "x"}).status_code == 400  # bad type
    assert c.post(f"/api/target/{tt}/{tid}/annotations", json={"content": "  "}).status_code == 400  # empty


def test_edit_and_delete_annotation(client):
    c, ids = client
    db = c.app.dependency_overrides[get_case_db_path]()
    nid, tt, tid = f"addr:{ids['addr']}", "address", ids["addr"]
    # THE headline invariant: editing/deleting a note must touch ONLY the `annotation` table — never a
    # fact or a sourced claim. Snapshot every OTHER table's full contents up front and re-check at the end.
    facts_before = _fingerprint(db, exclude={"annotation"})
    a1 = c.post(f"/api/target/{tt}/{tid}/annotations", json={"content": "first"}).json()["annotations"][0]["id"]
    c.post(f"/api/target/{tt}/{tid}/annotations", json={"content": "second"})

    # EDIT in place -> the endpoint returns the target's refreshed list
    r = c.patch(f"/api/annotations/{a1}", json={"content": "first (revised)"})
    assert r.status_code == 200 and r.json()["target_type"] == "address" and r.json()["target_id"] == tid
    contents = [a["content"] for a in r.json()["annotations"]]
    assert "first (revised)" in contents and "first" not in contents
    assert c.patch(f"/api/annotations/{a1}", json={"content": "  "}).status_code == 400      # empty
    assert c.patch("/api/annotations/ghost", json={"content": "x"}).status_code == 404        # unknown id

    # DELETE one -> the other remains; the node is still annotated (green outline persists)
    d = c.delete(f"/api/annotations/{a1}")
    assert d.status_code == 200 and len(d.json()["annotations"]) == 1
    assert c.delete(f"/api/annotations/{a1}").status_code == 404      # already gone -> idempotent 404
    g = c.get("/api/view", params={"focus": nid, "node_cap": 50}).json()
    assert any(n.get("has_annotation") for n in g["nodes"] if n["id"] == nid)

    # DELETE the last -> has_annotation clears (the read-model recomputes it; outline gone)
    last = c.get(f"/api/target/{tt}/{tid}/annotations").json()["annotations"][0]["id"]
    c.delete(f"/api/annotations/{last}")
    g2 = c.get("/api/view", params={"focus": nid, "node_cap": 50}).json()
    assert not any(n.get("has_annotation") for n in g2["nodes"] if n["id"] == nid)
    assert c.delete("/api/annotations/ghost").status_code == 404

    # the same id-keyed endpoints work for a FLOW (target-agnostic): annotate -> delete -> glow clears
    fa = c.post(f"/api/target/transfer/{ids['transfer']}/annotations",
                json={"content": "flow note"}).json()["annotations"][0]["id"]
    assert any(e.get("has_annotation") for e in c.get("/api/graph").json()["edges"]
               if e.get("ann_id") == ids["transfer"])
    c.delete(f"/api/annotations/{fa}")
    assert not any(e.get("has_annotation") for e in c.get("/api/graph").json()["edges"]
                   if e.get("ann_id") == ids["transfer"])

    # the guardrail: across every add/edit/delete above, NOT ONE non-annotation table row changed — no
    # fact and no sourced claim was mutated (Inv #4/#5; the test that fails if the invariant is broken).
    assert _fingerprint(db, exclude={"annotation"}) == facts_before
    # ...and edit/delete are Family C (not a fact mutation) — every audit stays green.
    assert all(rr.passed for rr in run_audits(db_path=db))


def test_deleting_an_annotated_finding_leaves_no_dangling_note(case):
    """A finding is an annotation TARGET (poly ref, no DB cascade). Deleting a finding that carries a note
    must remove that note too, or the note's target_id dangles and the no-dangling-fk audit fails."""
    conn, db, _ids = case
    from backend.app.services import investigator

    fid = investigator.create_finding(conn, statement="finding to annotate")
    investigator.add_annotation(conn, target_type="finding", target_id=fid, content="note about the finding")
    investigator.delete_finding(conn, finding_id=fid)
    assert conn.execute("SELECT COUNT(*) FROM annotation WHERE target_type='finding' AND target_id=?",
                        (fid,)).fetchone()[0] == 0
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --- C: findings composer ------------------------------------------------------------------

def test_findings_compose_edit_refs_delete(client):
    c, ids = client
    r = c.post("/api/findings", json={
        "statement": "Address A laundered via the BTC tx", "assessment": "high",
        "refs": [{"ref_type": "address", "ref_id": ids["addr"], "note": "origin"},
                 {"ref_type": "transaction", "ref_id": ids["tx"], "note": "hop"}]})
    assert r.status_code == 200
    fid = r.json()["finding_id"]
    f = next(x for x in c.get("/api/findings").json()["findings"] if x["id"] == fid)
    assert f["assessment"] == "high" and len(f["refs"]) == 2
    assert all(ref["label"] for ref in f["refs"])           # refs resolve to readable labels (jump-to)

    # edit the statement/assessment
    c.patch(f"/api/findings/{fid}", json={"statement": "Revised statement", "assessment": "medium"})
    f = next(x for x in c.get("/api/findings").json()["findings"] if x["id"] == fid)
    assert f["statement"] == "Revised statement" and f["assessment"] == "medium"

    # remove one ref, then delete the finding
    ref_id = f["refs"][0]["id"]
    c.delete(f"/api/findings/refs/{ref_id}")
    f = next(x for x in c.get("/api/findings").json()["findings"] if x["id"] == fid)
    assert len(f["refs"]) == 1
    c.delete(f"/api/findings/{fid}")
    assert all(x["id"] != fid for x in c.get("/api/findings").json()["findings"])

    assert c.post("/api/findings", json={"statement": "   "}).status_code == 400              # empty
    assert c.post("/api/findings", json={"statement": "x", "refs": [{"ref_type": "address",
                  "ref_id": "ghost"}]}).status_code == 400                                     # bad ref -> no orphan
    assert not c.get("/api/findings").json()["findings"]                                       # rolled back
    assert c.patch("/api/findings/ghost", json={"statement": "x"}).status_code == 404


def test_notes_aggregator(client):
    c, ids = client
    from backend.app.services import investigator
    nid = f"addr:{ids['addr']}"
    c.post(f"/api/node/{nid}/annotations", json={"content": "a note"})
    c.post(f"/api/address/{ids['addr']}/label", json={"label": "Mixer wallet"})  # investigator_label
    notes = c.get("/api/investigator/notes").json()["notes"]
    grp = next(g for g in notes if g["target_id"] == ids["addr"])
    assert grp["node_id"] == nid and grp["label"] == "Mixer wallet"
    assert grp["label_override"] == "Mixer wallet"
    assert any(a["content"] == "a note" for a in grp["annotations"])
    _ = investigator  # (module imported to assert the service path is wired)


# --- the architecture guardrail: view operations mutate ZERO case rows --------------------

def test_view_reset_and_navigation_delete_zero_rows(client):
    """Home / step-back / expand are pure view-param changes — requesting any of them must not add or
    delete a single case row. Investigator inputs persist across all of them."""
    c, ids = client
    nid = f"addr:{ids['addr']}"
    # durable inputs first
    c.post(f"/api/node/{nid}/annotations", json={"content": "durable note"})
    c.post("/api/findings", json={"statement": "durable finding", "refs": [
        {"ref_type": "address", "ref_id": ids["addr"]}]})

    conn2 = get_connection(c.app.dependency_overrides[get_case_db_path]())
    tables = [r["name"] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    snap = lambda: {t: conn2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    before = snap()

    # simulate Home + navigation + filter changes (all view-only)
    for params in ({}, {"focus": nid, "hops": 2}, {"focus": nid, "expand": "agg:%s:in" % nid},
                   {"group_dust": "false"}, {"value_floor_usd": 100}, {}):  # last {} == Home/default
        assert c.get("/api/view", params=params).status_code == 200
    after = snap()
    assert before == after                                  # not one row added or deleted by any view

    # the durable inputs survived every view
    assert c.get(f"/api/node/{nid}/annotations").json()["annotations"]
    assert c.get("/api/findings").json()["findings"]
    conn2.close()


# --- report: findings + investigator-notes appendix ----------------------------------------

def test_report_context_carries_findings_and_notes(case):
    conn, _db, ids = case
    from backend.app.services import investigator
    from backend.app.services.reporting import _findings_section, _notes_section, build_report_context

    investigator.add_annotation(conn, target_type="address", target_id=ids["addr"], content="watch this")
    investigator.set_label(conn, target_type="address", target_id=ids["addr"], label="Suspect-1")
    fid = investigator.create_finding(conn, statement="Key finding", assessment="high")
    investigator.add_finding_ref(conn, finding_id=fid, ref_type="address", ref_id=ids["addr"], note="origin")

    ctx = build_report_context(conn, title="R", scope_spec={}, generated_at="2026-01-01T00:00:00Z")
    assert ctx["findings"] and ctx["findings"][0]["statement"] == "Key finding"
    assert "Key finding" in _findings_section(ctx["findings"])
    notes_html = _notes_section(ctx["notes"])
    assert "watch this" in notes_html and "Suspect-1" in notes_html    # annotation + label in the appendix


def test_annotations_findings_survive_export_roundtrip(tmp_path):
    from backend.app.services import investigator
    from backend.app.services.export import export_case, verify_casefile

    conn, db = _seed(tmp_path)
    addr = conn.execute("SELECT id FROM address LIMIT 1").fetchone()["id"]
    aid = investigator.add_annotation(conn, target_type="address", target_id=addr, content="surviving note")
    investigator.update_annotation(conn, annotation_id=aid, content="surviving note (revised)")  # EDIT then export
    fid = investigator.create_finding(conn, statement="surviving finding")
    investigator.add_finding_ref(conn, finding_id=fid, ref_type="address", ref_id=addr)
    run_audits(db_path=str(db))
    conn.close()

    bundle = export_case(tmp_path)
    report = verify_casefile(bundle, extract_to=tmp_path / "ex")
    assert report["ok"], report
    rconn = get_connection(tmp_path / "ex" / "case.db")
    try:
        notes = investigator.list_annotations(rconn, target_type="address", target_id=addr)
        assert any(n["content"] == "surviving note (revised)" for n in notes)   # the EDITED text survives
        assert any(f["statement"] == "surviving finding" for f in investigator.list_findings(rconn))
    finally:
        rconn.close()
