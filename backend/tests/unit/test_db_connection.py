"""Unit tests for the SQLite connection helper PRAGMAs (phase_00 step 4)."""

from __future__ import annotations

from backend.app.db import get_connection


def test_pragmas_applied(tmp_path):
    conn = get_connection(tmp_path / "case.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "case.db"
    conn = get_connection(nested)
    try:
        assert nested.parent.is_dir()
    finally:
        conn.close()
