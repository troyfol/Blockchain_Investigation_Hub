"""Batch 6 (LOG-02 / LOG-03): the schema-version signal must be trustworthy, and opening a case DB
created by a NEWER app version must be refused, not silently operated on.

- LOG-02: ``schema_version`` was stamped ONCE at ``init_case`` and no migration updated it, so three
  identically-migrated DBs reported 5/3/1. ``apply_migrations`` must now re-stamp it.
- LOG-03: ``open_case`` ran ``apply_migrations`` with no version comparison; a case whose applied-migration
  set is ahead of the app must be refused with a clear message.
"""

from __future__ import annotations

import pytest

from backend.app.db import migrate
from backend.app.db.migrate import (
    CURRENT_SCHEMA_VERSION,
    SchemaTooNewError,
    apply_migrations,
    assert_supported_schema,
    read_schema_version,
)
from backend.tests.integration._helpers import new_case


def test_apply_migrations_restamps_schema_version(tmp_path):
    conn, db = new_case(tmp_path)
    # Simulate a pre-fix DB whose stamp is stale (init stamped CURRENT; force it to an older value).
    conn.execute("UPDATE case_meta SET schema_version = 1")
    conn.close()
    assert read_schema_version(db) == 1

    apply_migrations(db)  # a re-migrate must re-stamp the version to the applied migration set's level
    assert read_schema_version(db) == CURRENT_SCHEMA_VERSION, \
        "apply_migrations did not re-stamp schema_version (LOG-02)"


def test_refuse_case_db_newer_than_app(tmp_path, monkeypatch):
    conn, db = new_case(tmp_path)
    conn.close()
    # A normally-migrated case is fine.
    assert_supported_schema(db)  # no raise

    # Simulate an OLDER app: it doesn't know the newest migration the DB has applied → the DB is "ahead".
    full = migrate.known_migration_ids()
    newest = sorted(full)[-1]
    monkeypatch.setattr(migrate, "known_migration_ids", lambda: full - {newest})
    with pytest.raises(SchemaTooNewError):
        assert_supported_schema(db)


def test_open_case_refuses_newer_db(tmp_path, monkeypatch):
    from backend.app.services import cases

    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    cases.clear_active_case()
    res = cases.new_case("Newer")
    path = res["path"]
    cases.clear_active_case()

    full = migrate.known_migration_ids()
    newest = sorted(full)[-1]
    monkeypatch.setattr(migrate, "known_migration_ids", lambda: full - {newest})
    with pytest.raises(SchemaTooNewError):
        cases.open_case(path)
    cases.clear_active_case()
