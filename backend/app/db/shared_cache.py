"""Shared library cache (Phase 1, phase_01 step 7; docs/schema.md §6).

A SEPARATE SQLite DB (never inside a case folder) caching cross-case claims keyed by natural
keys. It is a pure performance optimization — never a runtime dependency of an opened case.

On use, a cached claim is copied into the active case **together with its originating
``source_query`` row** (so provenance FKs resolve and the case stays self-contained — audit #8).
Copying from cache is itself **not** a ``source_query``: the original query row is copied as-is,
preserving the original retrieval time. The claim's ``address_id`` is remapped to the case's
address row for the same ``(chain, address)`` natural key, and the claim keeps its original id so
re-copying is idempotent.

The address-only claims (attribution, risk_assessment) copy with just an ``address_id`` remap (+ their
source_query). ``balance_snapshot`` (FN-23) also remaps its ``asset_id``, CARRYING the asset (+ its
source_query) into the case when absent. ``valuation`` (subject FK to transfer/tx_output) is not copied on
this path — the valuation service copies a cached PRICE with its original source_query directly.

Every copy dedups on CONTENT, not just claim id: a content-identical claim already present from a live
fetch (or an earlier copy) is a no-op — ONE row, never two (Invariant #7). The content key includes
``source``, so a claim that differs on any substantive field — a different source, category, amount — is a
DISTINCT claim, kept side-by-side (Invariant #4); only genuinely identical claims collapse.
"""

from __future__ import annotations

import re

from .connection import get_connection
from .migrate import apply_migrations

# Address-only claim tables (their only non-self FK is address_id + source_query).
ADDRESS_CLAIM_TABLES = {"attribution", "risk_assessment"}
# Tables `_insert_row` may copy into (defense-in-depth whitelist). `asset` + `balance_snapshot` join via
# FN-23; `source_query` is the carried provenance row every claim references.
_COPYABLE_TABLES = ADDRESS_CLAIM_TABLES | {"source_query", "asset", "balance_snapshot"}
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# The SUBSTANTIVE columns that make a claim "the same claim" for content-dedup (Invariant #7) — i.e. every
# column EXCEPT the surrogate ``id``, the fetch-time ``retrieved_at``, and the ``source_query_id`` (all of
# which legitimately differ between a live fetch and a cached copy of the identical assertion). ``source``
# is deliberately IN the key: different sources are never merged (Invariant #4). Mirrors the natural key
# ``repository.upsert_attribution`` already enforces, extended to risk_assessment + balance_snapshot.
_CONTENT_COLUMNS = {
    "attribution":      ("address_id", "label", "category", "source", "confidence", "note"),
    "risk_assessment":  ("address_id", "score", "score_scale", "category", "source", "rationale"),
    "balance_snapshot": ("address_id", "asset_id", "amount", "as_of_ts", "source"),
}


def get_cache_connection(path):
    return get_connection(path)


# Cache-ONLY table (FN-05): the shared price cache. It is NOT part of the case schema — it lives only in
# the library cache DB, so adding it does NOT bump the case ``schema_version`` (case DBs never carry it).
# Keyed by (coin_key, requested unix ts). ``source_query_id`` FKs the cache's own source_query row (the
# ORIGINAL retrieval), which travels with a price so provenance is preserved when copied into a case.
_PRICE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS price_cache (
    coin_key         TEXT    NOT NULL,
    price_ts         INTEGER NOT NULL,   -- the REQUESTED unix timestamp (= the movement's block ts)
    unit_price       TEXT    NOT NULL,   -- Decimal TEXT (PriceRecord.price)
    price_timestamp  INTEGER NOT NULL,   -- the price's OWN unix timestamp (may differ from price_ts)
    confidence       REAL,
    retrieved_at     TEXT    NOT NULL,   -- original retrieval time (the source_query.requested_at)
    source_query_id  TEXT    NOT NULL REFERENCES source_query(id),
    PRIMARY KEY (coin_key, price_ts)
);
"""


def _ensure_price_cache(conn) -> None:
    conn.execute(_PRICE_CACHE_DDL)  # autocommit connection → persists immediately; idempotent (IF NOT EXISTS)


def migrate_cache(path) -> int:
    """The cache uses the same schema as a case DB, PLUS the cache-only ``price_cache`` table (FN-05)."""
    count = apply_migrations(path)
    conn = get_connection(path)
    try:
        _ensure_price_cache(conn)
    finally:
        conn.close()
    return count


def get_cached_price(cache_conn, coin_key: str, price_ts: int):
    """The cached price for ``(coin_key, price_ts)``, or ``None`` on a miss. Read-only."""
    return cache_conn.execute(
        "SELECT coin_key, price_ts, unit_price, price_timestamp, confidence, retrieved_at, "
        "source_query_id FROM price_cache WHERE coin_key=? AND price_ts=?",
        (coin_key, int(price_ts))).fetchone()


def put_cached_price(cache_conn, case_conn, *, coin_key: str, price_ts: int, unit_price: str,
                     price_timestamp: int, confidence, retrieved_at: str, source_query_id: str) -> None:
    """Write a freshly-fetched price into the shared cache for reuse, carrying the ORIGINATING
    ``source_query`` row (copied from the case) so the cache stays self-contained (its FK resolves).
    Idempotent on ``(coin_key, price_ts)`` — a second write for the same key is a no-op."""
    if cache_conn.execute("SELECT 1 FROM price_cache WHERE coin_key=? AND price_ts=?",
                          (coin_key, int(price_ts))).fetchone():
        return
    sp = "price_cache_put"
    cache_conn.execute(f"SAVEPOINT {sp}")
    try:
        if cache_conn.execute("SELECT 1 FROM source_query WHERE id=?",
                              (source_query_id,)).fetchone() is None:
            sq_row = case_conn.execute("SELECT * FROM source_query WHERE id=?",
                                       (source_query_id,)).fetchone()
            if sq_row is None:
                raise ValueError("source_query missing from the case when caching a price")
            _insert_row(cache_conn, "source_query", dict(sq_row))
        cache_conn.execute(
            "INSERT INTO price_cache (coin_key, price_ts, unit_price, price_timestamp, confidence, "
            "retrieved_at, source_query_id) VALUES (?,?,?,?,?,?,?)",
            (coin_key, int(price_ts), unit_price, int(price_timestamp), confidence, retrieved_at,
             source_query_id))
        cache_conn.execute(f"RELEASE {sp}")
    except Exception:
        cache_conn.execute(f"ROLLBACK TO {sp}")
        cache_conn.execute(f"RELEASE {sp}")
        raise


def copy_source_query_into_case(case_conn, cache_conn, source_query_id: str) -> None:
    """Copy a ``source_query`` row from the cache into the case (idempotent) so a cache-derived claim's
    provenance FK resolves and the case stays self-contained (audit #8). Preserves the original id +
    retrieval time — copying from cache is NOT itself a new query."""
    if case_conn.execute("SELECT 1 FROM source_query WHERE id=?", (source_query_id,)).fetchone():
        return
    sq_row = cache_conn.execute("SELECT * FROM source_query WHERE id=?", (source_query_id,)).fetchone()
    if sq_row is None:
        raise ValueError(f"source_query {source_query_id!r} missing from the cache")
    _insert_row(case_conn, "source_query", dict(sq_row))


def _insert_row(conn, table: str, row: dict) -> None:
    if table not in _COPYABLE_TABLES:
        raise ValueError(f"refusing to copy into non-whitelisted table {table!r}")
    cols = list(row.keys())
    # Defense-in-depth: column names come from a whitelisted-table SELECT *, but validate
    # them as plain identifiers anyway (they are interpolated into SQL).
    for c in cols:
        if not _IDENT_RE.match(c):
            raise ValueError(f"refusing to copy unsafe column name {c!r}")
    placeholders = ",".join("?" for _ in cols)
    collist = ",".join(cols)
    conn.execute(f"INSERT INTO {table} ({collist}) VALUES ({placeholders})", [row[c] for c in cols])


def _find_content_identical(conn, table: str, row: dict) -> str | None:
    """The id of a content-identical claim already in ``conn`` (matching every substantive column of
    ``row``, ``NULL``-safe), or ``None``. ``row`` must already carry the case-local remapped ids
    (address_id/asset_id) so the comparison is against the case's own rows."""
    clauses, params = [], []
    for c in _CONTENT_COLUMNS[table]:  # table is whitelisted; columns come from this constant
        v = row.get(c)
        if v is None:
            clauses.append(f"{c} IS NULL")
        else:
            clauses.append(f"{c}=?")
            params.append(v)
    hit = conn.execute(f"SELECT id FROM {table} WHERE " + " AND ".join(clauses), params).fetchone()
    return hit["id"] if hit else None


def copy_address_claim_into_case(case_conn, cache_conn, *, claim_table: str, claim_id: str) -> str:
    """Copy an address-only claim (+ its source_query) from cache into the case. Returns the id.

    Idempotent: the claim keeps its original id; if it (or its source_query) is already present
    in the case, that part is skipped. The case must already contain the claim's address (by
    natural key) — you ingest an address, then enrich it from cache.
    """
    if claim_table not in ADDRESS_CLAIM_TABLES:
        raise ValueError(f"{claim_table!r} is not an address-only claim table (v1)")

    claim = cache_conn.execute(
        f"SELECT * FROM {claim_table} WHERE id=?", (claim_id,)  # table from whitelist above
    ).fetchone()
    if claim is None:
        raise ValueError(f"claim {claim_id!r} not found in cache table {claim_table!r}")

    # Already copied? -> idempotent no-op.
    if case_conn.execute(f"SELECT 1 FROM {claim_table} WHERE id=?", (claim_id,)).fetchone():
        return claim_id

    addr = cache_conn.execute(
        "SELECT chain, address FROM address WHERE id=?", (claim["address_id"],)
    ).fetchone()
    if addr is None:
        raise ValueError("cache claim references an address missing from the cache")

    case_addr = case_conn.execute(
        "SELECT id FROM address WHERE chain=? AND address=?", (addr["chain"], addr["address"])
    ).fetchone()
    if case_addr is None:
        raise ValueError(
            f"target address {addr['chain']}:{addr['address']} is not in the case; ingest it first"
        )

    sqid = claim["source_query_id"]
    sq_row = None
    if sqid:
        sq_row = cache_conn.execute("SELECT * FROM source_query WHERE id=?", (sqid,)).fetchone()
        if sq_row is None:
            raise ValueError("cache claim references a source_query missing from the cache")

    new_claim = dict(claim)
    new_claim["address_id"] = case_addr["id"]  # remap to the case's address row (content-dedup uses it)
    # Content-dedup (Invariant #7): a content-identical claim already in the case — from a live fetch or an
    # earlier copy under a different id — means this is the SAME assertion; no second row.
    dup = _find_content_identical(case_conn, claim_table, new_claim)
    if dup is not None:
        return dup

    sp = "cache_copy"
    case_conn.execute(f"SAVEPOINT {sp}")
    try:
        # Carry the source_query first so the claim's FK resolves.
        if sqid and case_conn.execute("SELECT 1 FROM source_query WHERE id=?", (sqid,)).fetchone() is None:
            _insert_row(case_conn, "source_query", dict(sq_row))
        _insert_row(case_conn, claim_table, new_claim)
        case_conn.execute(f"RELEASE {sp}")
    except Exception:
        case_conn.execute(f"ROLLBACK TO {sp}")
        case_conn.execute(f"RELEASE {sp}")
        raise
    return claim_id


def _ensure_case_asset(case_conn, cache_conn, cache_asset_id: str) -> str:
    """Return the case's asset id for the cache asset's natural key ``(chain, contract_address)``, COPYING
    the asset (+ carrying its source_query) into the case when absent. Assets are benign reference data
    (unlike addresses, which are never injected), so carrying one keeps the case self-contained (audit #8)."""
    a = cache_conn.execute("SELECT * FROM asset WHERE id=?", (cache_asset_id,)).fetchone()
    if a is None:
        raise ValueError("cache balance_snapshot references an asset missing from the cache")
    existing = case_conn.execute(
        "SELECT id FROM asset WHERE chain=? AND COALESCE(contract_address,'')=COALESCE(?,'')",
        (a["chain"], a["contract_address"])).fetchone()
    if existing is not None:
        return existing["id"]
    row = dict(a)
    if row.get("source_query_id"):
        copy_source_query_into_case(case_conn, cache_conn, row["source_query_id"])
    _insert_row(case_conn, "asset", row)  # preserves the cache id — the case had no such asset
    return row["id"]


def copy_balance_snapshot_into_case(case_conn, cache_conn, *, snapshot_id: str) -> str:
    """Copy a ``balance_snapshot`` (+ its asset + source_query) from cache into the case (FN-23), remapping
    BOTH ``address_id`` (by ``(chain, address)``) and ``asset_id`` (by ``(chain, contract_address)``) to the
    case's rows. The case must already contain the snapshot's ADDRESS (ingest it first — an address is never
    injected, mirroring the address-claim copy); the ASSET is carried when absent. Idempotent by id AND by
    content (Invariant #7): a re-copy, or a content-identical claim already present from a live fetch, is a
    no-op — the existing row's id is returned, never a second row. A different ``source`` (or amount/as_of)
    is a DISTINCT claim, kept side-by-side (Invariant #4). Returns the id now present in the case."""
    snap = cache_conn.execute("SELECT * FROM balance_snapshot WHERE id=?", (snapshot_id,)).fetchone()
    if snap is None:
        raise ValueError(f"balance_snapshot {snapshot_id!r} not found in cache")
    if case_conn.execute("SELECT 1 FROM balance_snapshot WHERE id=?", (snapshot_id,)).fetchone():
        return snapshot_id  # already copied (idempotent by id)

    addr = cache_conn.execute("SELECT chain, address FROM address WHERE id=?",
                              (snap["address_id"],)).fetchone()
    if addr is None:
        raise ValueError("cache balance_snapshot references an address missing from the cache")
    case_addr = case_conn.execute("SELECT id FROM address WHERE chain=? AND address=?",
                                  (addr["chain"], addr["address"])).fetchone()
    if case_addr is None:
        raise ValueError(
            f"target address {addr['chain']}:{addr['address']} is not in the case; ingest it first")

    sp = "cache_copy_balance"
    case_conn.execute(f"SAVEPOINT {sp}")
    try:
        new = dict(snap)
        new["address_id"] = case_addr["id"]
        if snap["asset_id"] is not None:
            new["asset_id"] = _ensure_case_asset(case_conn, cache_conn, snap["asset_id"])
        # Content-dedup with the FINAL remapped ids. A dup implies its asset already existed (a just-copied
        # asset has no claims yet), so nothing carried above is orphaned when we return here.
        dup = _find_content_identical(case_conn, "balance_snapshot", new)
        if dup is not None:
            case_conn.execute(f"RELEASE {sp}")
            return dup
        if snap["source_query_id"]:
            copy_source_query_into_case(case_conn, cache_conn, snap["source_query_id"])
        _insert_row(case_conn, "balance_snapshot", new)
        case_conn.execute(f"RELEASE {sp}")
    except Exception:
        case_conn.execute(f"ROLLBACK TO {sp}")
        case_conn.execute(f"RELEASE {sp}")
        raise
    return snapshot_id
