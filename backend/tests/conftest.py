"""Shared pytest fixtures.

Note on empty suites: every gate (`make test`, `make smoke`) now collects real tests, so we
do NOT rewrite pytest's exit code 5 ("no tests collected"). With ``--strict-markers`` /
``--strict-config`` enabled (pyproject), a marker typo errors loudly and an accidental
zero-selection fails red rather than passing green — the safe default once tests are expected
to exist (docs/testing.md §6 requires a smoke test per phase).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.app.db import apply_migrations, get_connection


@pytest.fixture(autouse=True)
def _reset_jobs():
    """P8.7.2 — the long-operation jobs registry is process-global. Reset it around EVERY test so a job a
    test left active (running/canceled) can never leak into an unrelated test and spuriously cancel its
    connector calls or skew progress (the worker hooks raise JobCancelled off a stale canceled job)."""
    from backend.app.services import jobs

    jobs.clear()
    yield
    jobs.clear()


@pytest.fixture
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    """A migrated, empty case DB on a temp path (no migrations yet in Phase 0)."""
    db_path = tmp_path / "case.db"
    apply_migrations(db_path)
    conn = get_connection(db_path)
    yield conn
    conn.close()
