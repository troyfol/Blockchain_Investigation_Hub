"""P8.8 clustering heuristics — faithful, confidence-tagged, provenance-carrying, REVERSIBLE, side-by-side.

Covers the directed tests: each heuristic writes confidence-tagged memberships with a source_query and is
reversible (split + undo-run); a deposit-reuse fixture clusters two senders to one deposit; an
optimal_change fixture; CoinJoin still blocks cross-mixer merges; community detection is non-ownership +
connected and never writes a membership; "require ≥N agree" composition works.
"""

from __future__ import annotations

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import (
    Address, Asset, Attribution, SourceQuery, Transaction, Transfer, TxInput, TxOutput,
)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services import entities
from backend.app.services.clustering import btc_change, community, evm, service
from backend.tests.integration._helpers import new_case

BTC = 100_000_000  # sats per BTC


# --------------------------------------------------------------------------- seeders

def _seed_btc_tx(conn, *, txid, inputs, outputs, final=True):
    """inputs=[(addr, sats)], outputs=[(addr, sats, spent)] -> a BTC tx with EXACT input/output addresses."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        tx_id = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash=txid, block_height=800000, block_ts="2026-01-01T00:00:00Z",
            fee="1000", confirmations=11 if final else 1,
            finality_status="final" if final else "provisional"), sqid)
        for idx, (addr, sats) in enumerate(inputs):
            aid = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid)
            repo.upsert_tx_input(c, TxInput(transaction_id=tx_id, address_id=aid, amount=str(sats),
                                            input_index=idx), sqid)
        for idx, out in enumerate(outputs):
            addr, sats = out[0], out[1]
            spent = out[2] if len(out) > 2 else 0
            oid = repo.upsert_address(c, Address(chain="bitcoin", address_display=addr), sqid)
            repo.upsert_tx_output(c, TxOutput(transaction_id=tx_id, address_id=oid, amount=str(sats),
                                              output_index=idx, spent=spent), sqid)
        return tx_id

    _, tx_id = write_with_provenance(conn, sq, write)
    return tx_id


def _seed_evm(conn, *, txh, frm, to, amount, block, symbol="ETH", decimals=18, contract=None):
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": frm, "bounds": "default"}, requested_at="2026-01-01T00:00:00Z",
                     status="ok")

    def write(c, sqid):
        asset_id = repo.upsert_asset(c, Asset(chain="ethereum", symbol=symbol, decimals=decimals,
                                              contract_address=contract), sqid)
        fid = repo.upsert_address(c, Address(chain="ethereum", address_display=frm), sqid)
        tid = repo.upsert_address(c, Address(chain="ethereum", address_display=to), sqid)
        tx_id = repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash=txh, block_height=block, block_ts="2026-01-01T00:00:00Z",
            fee="0", status="1", confirmations=100, finality_status="final"), sqid)
        repo.upsert_transfer(c, Transfer(transaction_id=tx_id, chain="ethereum", from_address_id=fid,
                                         to_address_id=tid, asset_id=asset_id, amount=str(amount),
                                         transfer_type="erc20" if contract else "native", position=0), sqid)

    write_with_provenance(conn, sq, write)


def _attr_exchange(conn, addr):
    sq = SourceQuery(connector="misttrack", capability="get_attributions", endpoint="x",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=addr), sqid)
        repo.upsert_attribution(c, Attribution(address_id=aid, label="Exchange", category="exchange",
                                               source="misttrack", retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, write)


def _addr_id(conn, addr):
    return conn.execute("SELECT id FROM address WHERE address_display=? OR address=?", (addr, addr)).fetchone()[0]


def _active_members(conn, source):
    return conn.execute(
        "SELECT COUNT(*) FROM entity_membership m WHERE m.source=? AND NOT EXISTS "
        "(SELECT 1 FROM entity_membership_retraction r WHERE r.membership_id=m.id)", (source,)).fetchone()[0]


# --------------------------------------------------------------------------- BTC change: optimal_change unit

def test_optimal_change_heuristic_and_single_input_caveat():
    A = btc_change._addr_type
    # multi-input: change is the output smaller than the smallest input
    tx = btc_change._Tx(tx_id="t", inputs=[("A", 10 * BTC, "p2pkh"), ("B", 10 * BTC, "p2pkh")],
                        outputs=[btc_change._Output("o1", "X", 15 * BTC, "X", "p2pkh", False, True),
                                 btc_change._Output("o2", "C", 4 * BTC, "C", "p2pkh", False, True)])
    tx._min_input = 10 * BTC
    assert btc_change.h_optimal_change(tx) == {"o2"}            # only the < min-input output
    # single-input caveat (documented): every output qualifies
    tx1 = btc_change._Tx(tx_id="t1", inputs=[("A", 10 * BTC, "p2pkh")],
                         outputs=[btc_change._Output("o1", "X", 15 * BTC, "X", "p2pkh", False, True),
                                  btc_change._Output("o2", "C", 4 * BTC, "C", "p2pkh", False, True)])
    tx1._min_input = 10 * BTC
    assert btc_change.h_optimal_change(tx1) == {"o1", "o2"}
    assert A("bc1qxyz") == "p2wpkh" and A("3abc") == "p2sh" and A("1abc") == "p2pkh"


# --------------------------------------------------------------------------- BTC change: require-N-agree + reversible

def test_btc_change_require_n_agree_and_reversible(tmp_path):
    conn, _db = new_case(tmp_path, title="Change")
    # tx: inputs A,B (10 BTC each); outputs payment->X (15 BTC), change->A (reused, 4.8999 BTC, non-round).
    _seed_btc_tx(conn, txid="c" * 64, inputs=[("addrA", 10 * BTC), ("addrB", 10 * BTC)],
                 outputs=[("addrX", 15 * BTC + 1), ("addrA", 489_990_001)])
    names = ["address_reuse", "optimal_change"]

    # require 2 agree -> address_reuse(unique=change) AND optimal_change(unique=change) both pick change -> cluster {A,B}
    res = btc_change.cluster_btc_change(conn, heuristics=names, require_agree=2, now="2026-01-02T00:00:00Z")
    assert res["clusters"] == 1 and res["memberships_created"] == 2
    # confidence-tagged + provenance-carrying
    rows = conn.execute(
        "SELECT confidence, source, method, source_query_id FROM entity_membership WHERE source='btc-change-heuristic'"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["confidence"] is not None and r["confidence"] > 0 for r in rows)
    assert all(r["source_query_id"] == res["source_query_id"] for r in rows)
    assert all(r["method"].startswith("change:") for r in rows)

    # REVERSIBLE — split one address out (retraction), then undo the whole run
    mid = conn.execute("SELECT id FROM entity_membership WHERE source='btc-change-heuristic' LIMIT 1").fetchone()[0]
    entities.split_address(conn, membership_id=mid, reason="test-split")
    assert _active_members(conn, "btc-change-heuristic") == 1
    undo = service.undo_run(conn, res["source_query_id"])
    assert undo["retracted"] == 1                              # the remaining active one
    assert _active_members(conn, "btc-change-heuristic") == 0  # whole run reversed
    assert all(r.passed for r in run_audits(db_path=str(_db)))  # split + undo leave an audit-clean case


def test_btc_change_require_3_agree_forms_nothing(tmp_path):
    conn, _db = new_case(tmp_path, title="Change3")
    _seed_btc_tx(conn, txid="d" * 64, inputs=[("addrA", 10 * BTC), ("addrB", 10 * BTC)],
                 outputs=[("addrX", 15 * BTC + 1), ("addrA", 489_990_001)])
    # only 2 heuristics can agree -> require 3 -> no cluster (BlockSci: never a single bare heuristic)
    res = btc_change.cluster_btc_change(conn, heuristics=["address_reuse", "optimal_change"], require_agree=3)
    assert res["clusters"] == 0


# --------------------------------------------------------------------------- BTC change: CoinJoin gating

def test_btc_change_coinjoin_blocks_cross_mixer_merge(tmp_path):
    conn, _db = new_case(tmp_path, title="CJ")
    # a CoinJoin: 5 inputs + 5 equal outputs (one reuses input addrA so a change link WOULD otherwise form)
    ins = [(f"cj{i}", 10 * BTC) for i in range(5)]
    outs = [("cj0", 2 * BTC)] + [(f"o{i}", 2 * BTC) for i in range(4)]   # 5 equal outputs -> CoinJoin
    _seed_btc_tx(conn, txid="e" * 64, inputs=ins, outputs=outs)
    tx_id = conn.execute("SELECT id FROM transaction_").fetchone()[0]
    assert entities.is_probable_coinjoin(conn, tx_id) is True
    # the change heuristics must SKIP the CoinJoin tx -> no cross-mixer cluster
    res = btc_change.cluster_btc_change(conn, heuristics=["address_reuse", "optimal_change"], require_agree=1)
    assert res["clusters"] == 0


# --------------------------------------------------------------------------- EVM deposit-reuse fixture

def test_deposit_reuse_clusters_two_senders_to_one_deposit(tmp_path):
    conn, _db = new_case(tmp_path, title="Deposit")
    U1, U2, D, E = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "dd" * 20, "0x" + "ee" * 20
    _attr_exchange(conn, E)                                    # E is a known exchange
    _seed_evm(conn, txh="0x" + "a1" * 32, frm=U1, to=D, amount=10**18, block=100)   # U1 -> D (1 ETH)
    _seed_evm(conn, txh="0x" + "a2" * 32, frm=U2, to=D, amount=10**18, block=101)   # U2 -> D (1 ETH)
    _seed_evm(conn, txh="0x" + "a3" * 32, frm=D, to=E, amount=10**18 - 10**15, block=102)  # D -> E (~1 ETH)

    res = evm.cluster_deposit_reuse(conn, now="2026-01-02T00:00:00Z")
    assert res["clusters"] == 1
    assert all(r.passed for r in run_audits(db_path=str(_db)))   # clustering output is audit-clean
    # both senders clustered, confidence-tagged + provenance
    ent = entities.resolve(conn, conn.execute(
        "SELECT entity_id FROM entity_membership WHERE source='evm-deposit-reuse' LIMIT 1").fetchone()[0])
    members = {m["address_id"] for m in conn.execute(
        "SELECT address_id FROM entity_membership WHERE source='evm-deposit-reuse'").fetchall()}
    assert _addr_id(conn, U1) in members and _addr_id(conn, U2) in members
    assert _addr_id(conn, D) not in members and _addr_id(conn, E) not in members  # deposit/exchange excluded
    # a SIZE-2 deposit cluster is the minimal masquerade-susceptible shape -> reduced confidence + flag
    # ON THE WRITTEN memberships (the FC2020 false-positive mitigation reaching real output, not just preview)
    rows = conn.execute("SELECT confidence, flags, source_query_id FROM entity_membership WHERE source='evm-deposit-reuse'").fetchall()
    assert all(r["confidence"] == evm.MASQUERADE_REDUCED_CONFIDENCE and r["flags"] == "masquerade-risk"
               and r["source_query_id"] for r in rows)

    # reversible — undo the run
    assert _active_members(conn, "evm-deposit-reuse") == 2
    service.undo_run(conn, res["source_query_id"])
    assert _active_members(conn, "evm-deposit-reuse") == 0


def test_deposit_reuse_large_cluster_keeps_full_confidence(tmp_path):
    """A many-sender deposit cluster (size >= 3, which an adversary can't fabricate from one forward) keeps
    FULL confidence; a lone sender forms no cluster at all."""
    conn, _db = new_case(tmp_path, title="DepositBig")
    D, E = "0x" + "dd" * 20, "0x" + "ee" * 20
    _attr_exchange(conn, E)
    for i in range(3):                                          # three distinct senders -> size-3 cluster
        _seed_evm(conn, txh="0x" + f"{i:02x}" * 32, frm="0x" + f"{i+1:02x}" * 20, to=D, amount=10**18, block=100 + i)
    _seed_evm(conn, txh="0x" + "ff" * 32, frm=D, to=E, amount=10**18 - 10**15, block=110)
    res = evm.cluster_deposit_reuse(conn)
    assert res["clusters"] == 1
    rows = conn.execute("SELECT confidence, flags FROM entity_membership WHERE source='evm-deposit-reuse'").fetchall()
    assert len(rows) == 3
    assert all(r["confidence"] == evm.DEPOSIT_REUSE_CONFIDENCE and r["flags"] is None for r in rows)


def test_deposit_reuse_single_sender_forms_no_cluster(tmp_path):
    conn, _db = new_case(tmp_path, title="Masq")
    U, D, E = "0x" + "11" * 20, "0x" + "dd" * 20, "0x" + "ee" * 20
    _attr_exchange(conn, E)
    _seed_evm(conn, txh="0x" + "b1" * 32, frm=U, to=D, amount=10**18, block=100)
    _seed_evm(conn, txh="0x" + "b2" * 32, frm=D, to=E, amount=10**18 - 10**15, block=101)
    prev = evm.preview_deposit_reuse(conn)
    assert prev["n_clusters"] == 0    # a single sender forms no cluster on its own (>=2 needed)
    assert not prev["flags"]          # no cluster -> no per-cluster masquerade flag


# --------------------------------------------------------------------------- EVM self-authorization (data-gated)

def test_self_authorization_data_gated_then_clusters(tmp_path):
    conn, _db = new_case(tmp_path, title="SelfAuth")
    # no approval data -> clean no-op with an honest note
    out = evm.cluster_self_authorization(conn)
    assert out["clusters"] == 0 and "no ERC-20 Approval data" in out.get("note", "")
    # seed an Approval(owner, spender) directly into erc20_approval -> clusters owner<->spender
    O, S = "0x" + "01" * 20, "0x" + "02" * 20
    sq = SourceQuery(connector="approval-import", capability="get_approvals", endpoint="x",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        oid = repo.upsert_address(c, Address(chain="ethereum", address_display=O), sqid)
        sid = repo.upsert_address(c, Address(chain="ethereum", address_display=S), sqid)
        import uuid
        c.execute("INSERT INTO erc20_approval (id, chain, owner_address_id, spender_address_id, retrieved_at,"
                  " source_query_id) VALUES (?,?,?,?,?,?)",
                  (uuid.uuid4().hex, "ethereum", oid, sid, "2026-01-01T00:00:00Z", sqid))

    write_with_provenance(conn, sq, write)
    res = evm.cluster_self_authorization(conn)
    assert res["clusters"] == 1 and res["memberships_created"] == 2


# --------------------------------------------------------------------------- Leiden community (non-ownership)

def test_community_detection_is_connected_and_writes_no_membership(tmp_path):
    if not community.leiden_available():
        import pytest
        pytest.skip("python-igraph (Leiden) not installed")
    conn, _db = new_case(tmp_path, title="Community")
    # two tight triangles joined by a single edge -> two communities, each internally connected
    nodes = [{"id": f"addr:{c}{i}", "kind": "address"} for c in "AB" for i in range(3)]
    edges = []
    for c in "AB":
        ids = [f"addr:{c}{i}" for i in range(3)]
        edges += [{"source": ids[0], "target": ids[1]}, {"source": ids[1], "target": ids[2]},
                  {"source": ids[0], "target": ids[2]}]
    edges.append({"source": "addr:A0", "target": "addr:B0"})   # weak bridge
    before = conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0]
    res = community.detect_communities(nodes, edges)               # default modularity
    assert res["available"] and res["n_communities"] >= 2          # Leiden splits the two triangles
    assert "structure, not ownership" in res["note"]
    # community detection NEVER writes an ownership claim (Inv #3/#4) — it took no conn at all
    assert conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0] == before
    # each community is internally connected (the Leiden guarantee Louvain lacks)
    comm = res["communities"]
    by_c: dict = {}
    for nid, ci in comm.items():
        by_c.setdefault(ci, set()).add(nid)
    adj: dict = {}
    for e in edges:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])
    for members in by_c.values():
        # BFS within the community stays connected
        start = next(iter(members)); seen = {start}; stack = [start]
        while stack:
            x = stack.pop()
            for y in adj.get(x, ()):
                if y in members and y not in seen:
                    seen.add(y); stack.append(y)
        assert seen == members


# --------------------------------------------------------------------------- side-by-side + summary

def test_build_view_community_overlay_groups_but_persists_nothing(tmp_path):
    """The Leiden overlay renders as a DISTINCT group_type='community' in the view but writes no claim."""
    if not community.leiden_available():
        import pytest
        pytest.skip("python-igraph (Leiden) not installed")
    from backend.app.services.graph_view import build_view

    conn, _db = new_case(tmp_path, title="ViewCommunity")
    hub = "0x" + "00" * 20
    for i in range(4):                                  # a small star around the hub -> one community
        _seed_evm(conn, txh="0x" + f"{i:02x}" * 32, frm=hub, to="0x" + f"{i+1:02x}" * 20, amount=10**18, block=100 + i)
    before = conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0]
    v = build_view(conn, focus=hub, hops=1, node_cap=50, community_detect=True)
    groups = [n for n in v["nodes"] if n.get("group_type") == "community"]
    assert v["meta"]["community_note"] and "ownership" in v["meta"]["community_note"]
    assert conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0] == before  # nothing persisted
    # a control run WITHOUT the overlay has no community groups
    v0 = build_view(conn, focus=hub, hops=1, node_cap=50, community_detect=False)
    assert not any(n.get("group_type") == "community" for n in v0["nodes"])


# --------------------------------------------------------------------------- P8.8.1 default + names + report

def test_btc_change_default_requires_2_agree_and_opt_in_1_works(tmp_path):
    """The change-clustering DEFAULT requires ≥2 heuristics (a single heuristic must NOT merge — BlockSci's
    warning); ≥1 forms a cluster only as an explicit opt-in."""
    conn, _db = new_case(tmp_path, title="Default2")
    # only address_reuse uniquely identifies the change (change pays back a reused input address)
    _seed_btc_tx(conn, txid="a1" * 32, inputs=[("addrA", 10 * BTC), ("addrB", 10 * BTC)],
                 outputs=[("addrX", 7 * BTC), ("addrA", 489_990_001)])
    # default require_agree (omitted) -> a single heuristic can't reach 2 -> NO cluster
    assert btc_change.cluster_btc_change(conn, heuristics=["address_reuse"])["clusters"] == 0
    # explicit opt-in ≥1 -> the single heuristic forms the cluster
    assert btc_change.cluster_btc_change(conn, heuristics=["address_reuse"], require_agree=1)["clusters"] == 1


def test_cluster_display_names_are_descriptive(tmp_path):
    """Unnamed auto-clusters render a descriptive (kind + size) name, not '(unnamed … entity)'."""
    from backend.app.services import reporting
    from backend.app.services.entity_display import cluster_display_name

    conn, _db = new_case(tmp_path, title="Names")
    _seed_btc_tx(conn, txid="b2" * 32, inputs=[("nA", 10 * BTC), ("nB", 10 * BTC)],
                 outputs=[("nX", 15 * BTC + 1), ("nA", 489_990_001)])
    entities.cluster_cospend(conn)
    btc_change.cluster_btc_change(conn, heuristics=["address_reuse", "optimal_change"], require_agree=2)
    names = {e["name"] for e in reporting._collect_entities(conn)}
    assert any(n.startswith("Co-spend cluster (") and "address" in n for n in names)
    assert any(n.startswith("Change-heuristic cluster (") for n in names)
    assert cluster_display_name("heuristic-cluster", "evm-deposit-reuse", 3) == "Deposit-reuse cluster (3 addresses)"


def test_entities_section_summarizes_not_one_row_per_member(tmp_path):
    """A cluster renders as a SUMMARY (concise method + count) + a compact member list — NOT one row per
    member repeating the full method string."""
    from backend.app.services import reporting

    conn, _db = new_case(tmp_path, title="Summarize")
    _seed_btc_tx(conn, txid="c3" * 32, inputs=[("mA", 10 * BTC), ("mB", 10 * BTC)],
                 outputs=[("mX", 15 * BTC + 1), ("mA", 489_990_001)])
    btc_change.cluster_btc_change(conn, heuristics=["address_reuse", "optimal_change"], require_agree=2)
    html = reporting._entities_section(reporting._collect_entities(conn))
    assert "BTC change (2 heuristics, ≥2 agree)" in html        # the concise method label, once
    assert "change:address_reuse+optimal_change" not in html     # the raw concatenation is NOT printed
    assert "address" in html and "confidence" in html            # summary header carries count + confidence
    # the per-member method/confidence is not repeated as table rows
    assert "<th>method</th>" not in html


def test_cluster_summary_is_side_by_side(tmp_path):
    conn, _db = new_case(tmp_path, title="Summary")
    # co-spend (always-on) + a change heuristic on the SAME btc tx -> two side-by-side cluster claims
    _seed_btc_tx(conn, txid="f" * 64, inputs=[("sA", 10 * BTC), ("sB", 10 * BTC)],
                 outputs=[("sX", 15 * BTC + 1), ("sA", 489_990_001)])
    entities.cluster_cospend(conn)
    btc_change.cluster_btc_change(conn, heuristics=["address_reuse", "optimal_change"], require_agree=2)
    summ = service.cluster_summary(conn)
    assert "cospend-heuristic" in summ and "btc-change-heuristic" in summ   # both present, never merged
    assert summ["btc-change-heuristic"]["clusters"][0]["confidence_min"] is not None
