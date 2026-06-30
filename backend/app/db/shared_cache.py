"""Shared library cache (Phase 1, phase_01 step 7; docs/schema.md §6).

A SEPARATE SQLite DB (never inside a case folder) caching cross-case claims keyed by natural
keys. It is a pure performance optimization — never a runtime dependency of an opened case.

On use, a cached claim is copied into the active case **together with its originating
``source_query`` row** (so provenance FKs resolve and the case stays self-contained — audit #8).
Copying from cache is itself **not** a ``source_query``: the original query row is copied as-is,
preserving the original retrieval time. The claim's ``address_id`` is remapped to the case's
address row for the same ``(chain, address)`` natural key, and the claim keeps its original id so
re-copying is idempotent.

v1 supports the address-only claims (attribution, risk_assessment) — those whose only non-self
FK is ``address_id`` (+ source_query). ``balance_snapshot`` (asset_id FK) and ``valuation``
(subject FK to transfer/tx_output) need additional remapping and are deferred to Phase 5, when
those claims are actually cached.
"""

from __future__ import annotations

import re

from .connection import get_connection
from .migrate import apply_migrations

# Address-only claim tables copyable in v1 (no FK beyond address_id + source_query).
ADDRESS_CLAIM_TABLES = {"attribution", "risk_assessment"}
_COPYABLE_TABLES = ADDRESS_CLAIM_TABLES | {"source_query"}
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def get_cache_connection(path):
    return get_connection(path)


def migrate_cache(path) -> int:
    """The cache uses the same schema as a case DB."""
    return apply_migrations(path)


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

    sp = "cache_copy"
    case_conn.execute(f"SAVEPOINT {sp}")
    try:
        # Carry the source_query first so the claim's FK resolves.
        if sqid and case_conn.execute("SELECT 1 FROM source_query WHERE id=?", (sqid,)).fetchone() is None:
            _insert_row(case_conn, "source_query", dict(sq_row))
        new_claim = dict(claim)
        new_claim["address_id"] = case_addr["id"]  # remap to the case's address row
        _insert_row(case_conn, claim_table, new_claim)
        case_conn.execute(f"RELEASE {sp}")
    except Exception:
        case_conn.execute(f"ROLLBACK TO {sp}")
        case_conn.execute(f"RELEASE {sp}")
        raise
    return claim_id
