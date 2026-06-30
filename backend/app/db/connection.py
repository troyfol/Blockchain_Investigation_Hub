"""SQLite connection helper (phase_00 step 4, docs/schema.md §1).

Every connection applies the mandated PRAGMAs: foreign keys ON (so the audit's
no-dangling-FK check is meaningful), WAL journaling, and a 5s busy timeout.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PRAGMAS = (
    "PRAGMA foreign_keys = ON;",
    "PRAGMA journal_mode = WAL;",
    "PRAGMA busy_timeout = 5000;",
)


def get_connection(db_path: str | Path, *, create_parents: bool = True) -> sqlite3.Connection:
    """Open ``db_path`` with project PRAGMAs and a ``sqlite3.Row`` row factory.

    Parent directories are created by default so a brand-new case DB just works.
    """
    path = Path(db_path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI runs sync endpoints + their (yield) dependencies in an anyio
    # threadpool that may hop worker threads WITHIN a single request, so a connection opened in the
    # dependency would otherwise raise "created in a different thread" when the endpoint queries it.
    # Safe here: a connection is per-request and never used by two threads CONCURRENTLY (single-user,
    # sequential handler) — we only disable the thread-identity assertion, not add real concurrency.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Autocommit mode: statements commit immediately UNLESS wrapped in an explicit
    # BEGIN..COMMIT. This gives the provenance writer (provenance/atomic.py) precise control
    # so a source_query and its facts/claims commit in ONE transaction (Invariant #3).
    conn.isolation_level = None
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn
