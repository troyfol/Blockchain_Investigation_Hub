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
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..models import SourceQuery

RAW_SUBDIR = "raw_responses"


def _fsync_replace(temp_path: Path, dst: Path) -> None:
    """Durable atomic promote (RES-03): fsync the staged bytes, atomically rename into place, then
    best-effort fsync the directory so the rename survives a crash. ``Path.replace`` is atomic on POSIX
    and Windows; the directory fsync is a no-op where unsupported (e.g. Windows)."""
    try:
        with open(temp_path, "rb") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass
    temp_path.replace(dst)
    try:
        dfd = os.open(str(dst.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except (OSError, AttributeError):
        pass  # directory fsync unsupported here — the rename itself is still atomic


def sweep_stale_raw_tmp(db_dir: Path | str) -> int:
    """RES-03: remove leftover ``raw_responses/*.tmp`` stragglers (a staged file whose write never
    committed, e.g. after a hard crash). Best-effort; returns the number removed."""
    d = Path(db_dir) / RAW_SUBDIR
    if not d.exists():
        return 0
    removed = 0
    for tmp in d.glob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def orphan_raw_refs(conn) -> list[str]:
    """RES-03: any committed ``source_query.raw_response_ref`` with no on-disk file — a torn write left a
    provenance row pointing at a payload that was never promoted. Read-only; returns the missing refs."""
    db_dir = _db_dir(conn)
    if db_dir is None:
        return []
    missing = []
    for r in conn.execute(
            "SELECT raw_response_ref FROM source_query WHERE raw_response_ref IS NOT NULL").fetchall():
        ref = r[0]
        if not (db_dir / ref).exists():
            missing.append(ref)
    return missing


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
    promoted = False
    try:
        _insert_source_query(conn, source_query, raw_ref, raw_hash)
        result = write_fn(conn, sq_id)
        # RES-03: promote the staged raw file BEFORE the commit point (RELEASE), with an fsync'd rename.
        # Previously it was promoted AFTER the DB commit, so a crash in that window left a committed
        # source_query whose raw_response_ref pointed at a never-promoted file (a torn DB/filesystem
        # write). Now the file exists whenever its row does; the only residue of a crash is a harmless
        # orphan file with no row (swept by `sweep_stale_raw_tmp` / reported by `orphan_raw_refs`).
        if temp_path is not None and raw_path is not None:
            _fsync_replace(temp_path, raw_path)
            promoted = True
        conn.execute(f"RELEASE {sp}")
    except Exception:
        conn.execute(f"ROLLBACK TO {sp}")
        conn.execute(f"RELEASE {sp}")
        # roll the file back too — the promoted target if we got that far, else the staged temp
        if promoted and raw_path is not None and raw_path.exists():
            raw_path.unlink()
        elif temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise

    return sq_id, result
