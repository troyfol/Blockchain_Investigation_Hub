"""P5 / FN-05 — the shared-library price cache.

A price fetched once (coin + timestamp) is cached in the shared library DB so a later valuation of the
SAME (asset, ts) — in this case or a fresh one — reuses it with ZERO DeFiLlama calls. A cache hit copies
the ORIGINAL ``source_query`` into the case (so provenance reflects the original retrieval, audit #8),
never a synthesized "cache" query. A missing price caches nothing (honest gap). Invariants #3/#4 hold.
"""

from __future__ import annotations

from backend.app.connectors.defillama import PriceRecord
from backend.app.db import repository as repo
from backend.app.db.shared_cache import get_cache_connection, migrate_cache
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.valuation import value_movements
from backend.tests.integration._helpers import new_case


class StubPrices:
    """A price connector stub that returns a fixed native price and COUNTS its network calls. With
    ``calls_allowed=False`` it fails loudly if called — proving a full cache hit does zero network."""

    def __init__(self, price="2000", *, calls_allowed=True):
        self.price = price
        self.calls_allowed = calls_allowed
        self.calls = 0

    def coin_key(self, chain, asset):
        return f"coingecko:{chain}"  # native coin key (ignores the token path — these seeds are native)

    def get_prices(self, items, timestamp):
        self.calls += 1
        assert self.calls_allowed, "get_prices must NOT be called when every coin is a cache hit"
        out = {}
        for chain, asset in items:
            k = self.coin_key(chain, asset)
            out[k] = PriceRecord(key=k, price=self.price, symbol="ETH", decimals=None,
                                 price_timestamp=int(timestamp), confidence=0.99, raw={})
        return out, b'{"coins":{}}'


def _seed_movement(conn, *, block_ts="2026-01-01T00:00:00Z"):
    """A single native ETH transfer at a fixed block ts → exactly one unvalued movement."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "seed"}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        a = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "ab" * 20), sqid)
        b = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "cd" * 20), sqid)
        tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "e" * 64,
            block_ts=block_ts, confirmations=100, finality_status="final"), sqid)
        repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=a,
            to_address_id=b, asset_id=asset, amount="1000000000000000000", transfer_type="native",
            position=0), sqid)

    write_with_provenance(conn, sq, w)


def test_second_valuation_hits_cache_no_network(tmp_path):
    cache = get_cache_connection(_migrated_cache(tmp_path))

    # Case 1 — first valuation makes ONE network call and populates the cache.
    conn1, _ = new_case(tmp_path / "c1", title="Case1")
    _seed_movement(conn1)
    c1 = StubPrices(price="2000")
    r1 = value_movements(conn1, c1, now="2026-02-01T00:00:00Z", cache_conn=cache)
    assert r1["valued"] == 1 and c1.calls == 1

    # Case 2 (FRESH) — same (coin, ts). The connector MUST NOT be called: served entirely from cache.
    conn2, _ = new_case(tmp_path / "c2", title="Case2")
    _seed_movement(conn2)
    c2 = StubPrices(calls_allowed=False)
    r2 = value_movements(conn2, c2, now="2026-03-01T00:00:00Z", cache_conn=cache)
    assert r2["valued"] == 1 and c2.calls == 0  # ZERO DeFiLlama calls

    v = conn2.execute("SELECT unit_price, source FROM valuation").fetchone()
    assert v["unit_price"] == "2000" and v["source"] == "defillama"
    conn1.close(); conn2.close(); cache.close()


def test_cache_copy_carries_original_source_query(tmp_path):
    cache = get_cache_connection(_migrated_cache(tmp_path))

    conn1, _ = new_case(tmp_path / "c1", title="Case1")
    _seed_movement(conn1)
    value_movements(conn1, StubPrices(), now="2026-02-01T00:00:00Z", cache_conn=cache)
    orig_sqid = conn1.execute("SELECT source_query_id FROM valuation").fetchone()[0]
    orig = conn1.execute("SELECT connector, requested_at FROM source_query WHERE id=?",
                         (orig_sqid,)).fetchone()

    conn2, _ = new_case(tmp_path / "c2", title="Case2")
    _seed_movement(conn2)
    value_movements(conn2, StubPrices(calls_allowed=False), now="2026-03-01T00:00:00Z", cache_conn=cache)

    # The cache-hit valuation references the SAME original source_query (id + requested_at) — a real
    # 'defillama' retrieval, NOT a synthesized "cache" query.
    v2_sqid = conn2.execute("SELECT source_query_id FROM valuation").fetchone()[0]
    assert v2_sqid == orig_sqid
    sq2 = conn2.execute("SELECT connector, requested_at FROM source_query WHERE id=?",
                        (v2_sqid,)).fetchone()
    assert sq2["connector"] == "defillama"
    assert sq2["requested_at"] == orig["requested_at"] == "2026-02-01T00:00:00Z"
    conn1.close(); conn2.close(); cache.close()


def test_missing_price_caches_nothing(tmp_path):
    """An honest gap: a coin with no price writes no valuation AND caches nothing (never a fabricated zero)."""
    cache = get_cache_connection(_migrated_cache(tmp_path))
    conn, _ = new_case(tmp_path / "c", title="Case")
    _seed_movement(conn)

    class NoPrice(StubPrices):
        def get_prices(self, items, timestamp):
            self.calls += 1
            return {self.coin_key(c, a): None for c, a in items}, b'{"coins":{}}'

    res = value_movements(conn, NoPrice(), now="2026-02-01T00:00:00Z", cache_conn=cache)
    assert res["valued"] == 0 and res["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0
    assert cache.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0] == 0  # nothing cached
    conn.close(); cache.close()


def test_no_cache_conn_is_unchanged_behavior(tmp_path):
    """Back-compat: without a cache_conn, valuation behaves exactly as before (one fetch, one valuation)."""
    conn, _ = new_case(tmp_path / "c", title="Case")
    _seed_movement(conn)
    c = StubPrices()
    res = value_movements(conn, c, now="2026-02-01T00:00:00Z")  # no cache_conn
    assert res["valued"] == 1 and c.calls == 1
    conn.close()


def _migrated_cache(tmp_path):
    cache_path = tmp_path / "library_cache.db"
    migrate_cache(cache_path)
    return cache_path
