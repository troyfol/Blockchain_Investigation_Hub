"""Atomic provenance writer (Phase 1, phase_01 step 3).

``write_with_provenance`` is the ONE way facts/claims enter the DB. It opens a single
transaction (a SAVEPOINT, so it is nesting-safe and composable across connectors), inserts
the ``source_query`` row (with the raw response's SHA-256 in ``raw_response_hash``), then runs
the caller's ``write_fn`` to insert/upsert the fact/claim rows — all referencing that
``source_query`` — and commits atomically. A failure rolls the whole thing back. The raw
response file is staged to a temp path and only **promoted (atomic rename) after the DB
commit**, so the DB transaction is the single commit point: on rollback nothing is left behind;
on success the file matches the committed ``raw_response_hash``.

For an in-memory DB (no filesystem) the hash is still computed and stored, but
``raw_response_ref`` is NULL and no file is written.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..models import SourceQuery

RAW_SUBDIR = "raw_responses"


def _to_bytes(raw_response: bytes | str | dict | list) -> bytes:
    if isinstance(raw_response, bytes):
        return raw_response
    if isinstance(raw_response, str):
        return raw_response.encode("utf-8")
    # dict/list -> deterministic JSON so the hash is reproducible.
    return json.dumps(raw_response, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _db_dir(conn) -> Path | None:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file).parent if file else None
    return None


def _insert_source_query(conn, sq: SourceQuery, raw_ref: str | None, raw_hash: str | None) -> None:
    conn.execute(
        """
        INSERT INTO source_query
          (id, connector, capability, endpoint, params, requested_at, completed_at,
           status, raw_response_ref, raw_response_hash, result_summary)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            sq.id,
            sq.connector,
            sq.capability,
            sq.endpoint,
            json.dumps(sq.params) if sq.params is not None else None,
            sq.requested_at,
            sq.completed_at,
            sq.status,
            raw_ref,
            raw_hash,
            sq.result_summary,
        ),
    )


def write_with_provenance(
    conn,
    source_query: SourceQuery,
    write_fn: Callable[[Any, str], Any],
    *,
    raw_response: bytes | str | dict | list | None = None,
) -> tuple[str, Any]:
    """Write ``source_query`` + whatever ``write_fn(conn, source_query_id)`` inserts, atomically.

    Returns ``(source_query_id, write_fn_result)``. Raises (after full rollback) on any error.
    Safe to nest (uses a uniquely-named SAVEPOINT).
    """
    sq_id = source_query.id
    raw_ref: str | None = None
    raw_hash: str | None = None
    raw_path: Path | None = None
    temp_path: Path | None = None

    if raw_response is not None:
        raw_bytes = _to_bytes(raw_response)
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        db_dir = _db_dir(conn)
        if db_dir is not None:
            raw_path = db_dir / RAW_SUBDIR / f"{sq_id}.json"
            raw_ref = f"{RAW_SUBDIR}/{sq_id}.json"
            # Stage the bytes now; promote only after the DB commit succeeds.
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = raw_path.with_suffix(".json.tmp")
            temp_path.write_bytes(raw_bytes)

    sp = "sp_" + uuid4().hex[:12]
    conn.execute(f"SAVEPOINT {sp}")
    try:
        _insert_source_query(conn, source_query, raw_ref, raw_hash)
        result = write_fn(conn, sq_id)
        conn.execute(f"RELEASE {sp}")
    except Exception:
        conn.execute(f"ROLLBACK TO {sp}")
        conn.execute(f"RELEASE {sp}")
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise

    # Committed — promote the staged raw file into place (atomic rename).
    if temp_path is not None and raw_path is not None:
        temp_path.replace(raw_path)
    return sq_id, result
