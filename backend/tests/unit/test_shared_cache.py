"""P8 / FN-23 — shared-cache copy for `balance_snapshot` + content-based dedup.

The cache-copy path gains two things: (1) `balance_snapshot` becomes copyable — its `address_id` is remapped
to the case's address (never injected — ingest it first, like an address claim) and its `asset_id` is
remapped to the case's asset, CARRYING the asset (+ its source_query) when the case lacks it; (2) the copy
dedups on CONTENT, not just claim id, so a content-identical live + cached claim yields ONE row, not two
(Invariant #7) — while a claim differing on any substantive field, e.g. `source`, is kept side-by-side
(Invariant #4). Re-copy stays an idempotent no-op.
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.db.connection import get_connection
from backend.app.db.shared_cache import (
    copy_address_claim_into_case,
    copy_balance_snapshot_into_case,
    migrate_cache,
)
from backend.app.models import Address, Asset, Attribution, BalanceSnapshot, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

ADDR = "0x52908400098527886E0F7030069857D2E4169EE7"   # a checksummed EVM address (canonicalized on ingest)
USDT = "0x" + "d1" * 20                                 # lowercase-canonical token contract


def _cache(tmp_path):
    path = tmp_path / "library_cache.db"
    migrate_cache(path)
    return get_connection(path)


def _seed_case_address(conn, display=ADDR):
    """Put an address into the case (its own source_query → an id distinct from the cache's). Returns id."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": display, "bounds": "default"}, requested_at="2026-01-02T00:00:00Z",
                     status="ok")
    out = {}
    write_with_provenance(conn, sq,
                          lambda c, sqid: out.setdefault("id", repo.upsert_address(
                              c, Address(chain="ethereum", address_display=display), sqid)))
    return out["id"]


def _seed_cache_balance(cache, *, source="debank", amount="1000000", as_of="2026-01-01T00:00:00Z",
                        retrieved="2026-01-01T00:00:00Z", with_asset=True):
    """Seed a balance_snapshot (+ address + optional asset + source_query) into the CACHE. Returns ids."""
    sq = SourceQuery(connector=source, capability="get_balances", endpoint="balances",
                     params={"bounds": "default"}, requested_at=retrieved, status="ok")
    out = {}

    def w(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid)
        asset_id = repo.upsert_asset(
            c, Asset(chain="ethereum", contract_address=USDT, symbol="USDT", decimals=6), sqid) if with_asset else None
        out["snap"] = repo.insert_balance_snapshot(c, BalanceSnapshot(
            address_id=aid, asset_id=asset_id, amount=amount, as_of_ts=as_of, source=source,
            retrieved_at=retrieved), sqid)
        out["cache_addr"] = aid
        out["cache_asset"] = asset_id

    write_with_provenance(cache, sq, w, raw_response="cache raw")
    return out


def _live_case_balance(conn, *, source="debank", amount="1000000", as_of="2026-01-01T00:00:00Z",
                       retrieved="2026-02-01T00:00:00Z"):
    """Seed a LIVE balance_snapshot (+ address + asset) directly in the CASE. Returns ids."""
    sq = SourceQuery(connector=source, capability="get_balances", endpoint="balances",
                     params={"bounds": "default"}, requested_at=retrieved, status="ok")
    out = {}

    def w(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid)
        asset_id = repo.upsert_asset(
            c, Asset(chain="ethereum", contract_address=USDT, symbol="USDT", decimals=6), sqid)
        out["snap"] = repo.insert_balance_snapshot(c, BalanceSnapshot(
            address_id=aid, asset_id=asset_id, amount=amount, as_of_ts=as_of, source=source,
            retrieved_at=retrieved), sqid)
        out["addr"] = aid

    write_with_provenance(conn, sq, w)
    return out


def test_balance_snapshot_copy_remaps_asset(tmp_path):
    conn, db = new_case(tmp_path / "c", title="Balances")
    case_addr = _seed_case_address(conn)          # case has the address but NOT the asset
    cache = _cache(tmp_path)
    seeded = _seed_cache_balance(cache)

    new_id = copy_balance_snapshot_into_case(conn, cache, snapshot_id=seeded["snap"])
    cache.close()

    row = conn.execute("SELECT * FROM balance_snapshot WHERE id=?", (new_id,)).fetchone()
    # address remapped to the CASE's row (distinct id from the cache's), asset CARRIED + remapped.
    assert row["address_id"] == case_addr and case_addr != seeded["cache_addr"]
    case_asset = conn.execute(
        "SELECT id FROM asset WHERE chain='ethereum' AND contract_address=?", (USDT,)).fetchone()
    assert case_asset is not None and row["asset_id"] == case_asset["id"]
    # the originating source_query was carried → provenance resolves, no dangling FK.
    assert conn.execute("SELECT 1 FROM source_query WHERE id=?", (row["source_query_id"],)).fetchone()
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


def test_content_identical_claim_not_duplicated(tmp_path):
    conn, db = new_case(tmp_path / "c", title="Balances")
    live = _live_case_balance(conn)               # a live claim already in the case
    cache = _cache(tmp_path)
    # a CONTENT-identical claim (same addr/asset/amount/as_of/source) but a different id + retrieval time.
    seeded = _seed_cache_balance(cache, retrieved="2025-11-01T00:00:00Z")
    assert seeded["snap"] != live["snap"]

    returned = copy_balance_snapshot_into_case(conn, cache, snapshot_id=seeded["snap"])
    cache.close()

    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot").fetchone()[0] == 1   # ONE row, not two
    assert returned == live["snap"]                                                   # points at the existing
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


def test_balance_snapshot_copy_is_idempotent(tmp_path):
    conn, _ = new_case(tmp_path / "c", title="Balances")
    _seed_case_address(conn)
    cache = _cache(tmp_path)
    seeded = _seed_cache_balance(cache)

    id1 = copy_balance_snapshot_into_case(conn, cache, snapshot_id=seeded["snap"])
    id2 = copy_balance_snapshot_into_case(conn, cache, snapshot_id=seeded["snap"])  # same cached row again
    cache.close()
    assert id1 == id2 == seeded["snap"]
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot").fetchone()[0] == 1
    conn.close()


def test_different_source_balance_kept_side_by_side(tmp_path):
    conn, db = new_case(tmp_path / "c", title="Balances")
    _live_case_balance(conn, source="debank")     # live claim from debank
    cache = _cache(tmp_path)
    seeded = _seed_cache_balance(cache, source="zerion")  # a DIFFERENT source, same numbers

    copy_balance_snapshot_into_case(conn, cache, snapshot_id=seeded["snap"])
    cache.close()
    # disagreeing/agreeing sources are NEVER merged (Invariant #4) — both rows coexist.
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot").fetchone()[0] == 2
    assert {r[0] for r in conn.execute("SELECT DISTINCT source FROM balance_snapshot")} == {"debank", "zerion"}
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


def test_content_identical_attribution_not_duplicated(tmp_path):
    """The content-dedup also covers the address-claim path (previously id-only)."""
    conn, db = new_case(tmp_path / "c", title="Attr")
    live_addr = _seed_case_address(conn)
    # a live attribution already in the case.
    lsq = SourceQuery(connector="arkham", capability="get_attributions", endpoint="import",
                      params={"bounds": "default"}, requested_at="2026-02-01T00:00:00Z", status="ok")
    live_attr = {}
    write_with_provenance(conn, lsq, lambda c, sqid: live_attr.setdefault("id", repo.insert_attribution(
        c, Attribution(address_id=live_addr, label="Kraken", category="exchange", source="arkham",
                       confidence=0.8, retrieved_at="2026-02-01T00:00:00Z"), sqid)))

    cache = _cache(tmp_path)
    csq = SourceQuery(connector="arkham", capability="get_attributions", endpoint="import",
                      params={"bounds": "default"}, requested_at="2025-11-01T00:00:00Z", status="ok")
    cache_attr = {}
    write_with_provenance(cache, csq, lambda c, sqid: cache_attr.setdefault("id", repo.insert_attribution(
        c, Attribution(address_id=repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid),
                       label="Kraken", category="exchange", source="arkham", confidence=0.8,
                       retrieved_at="2025-11-01T00:00:00Z"), sqid)), raw_response="x")

    returned = copy_address_claim_into_case(conn, cache, claim_table="attribution", claim_id=cache_attr["id"])
    cache.close()
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 1   # content-identical → one row
    assert returned == live_attr["id"]
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()
