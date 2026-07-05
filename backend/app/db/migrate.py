"""Forward-only migration runner (phase_00 step 4).

Applies the yoyo SQL migrations under ``backend/app/migrations`` to a target case DB and
reads/writes ``case_meta.schema_version``. In Phase 0 there are no migration files, so this is
a green no-op; the runner must still work against a fresh DB.

**Policy:** migrations are DDL-only (CREATE TABLE/INDEX/VIEW per docs/schema.md). Runtime data
writes go through ``get_connection`` (FK enforcement ON). As defence in depth we still force
``PRAGMA foreign_keys = ON`` on yoyo's own connection (it defaults OFF and yoyo does not set
it), so any FK-sensitive migration step is enforced rather than silently skipped.

Run: ``python -m backend.app.db.migrate <db_path>``  (or ``bih-migrate <db_path>``).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from yoyo import get_backend, read_migrations

from ..app_paths import resource_path
from .connection import get_connection

# The yoyo SQL migrations — a bundled READ-ONLY resource (P7): _MEIPASS/backend/app/migrations when
# frozen, else the in-repo dir in source (via resource_path).
MIGRATIONS_DIR = resource_path("backend/app/migrations")

# The schema version the current migration set produces. Bump when a phase adds migrations.
# Phase 1 ships migrations 0001..0005 -> version 1. The GraphSense ActorPack importer adds 0006
# (entity.external_id) -> version 2. Cross-source transfer reconciliation adds 0007 (transfer.occurrence
# + content-based ux_transfer) -> version 3. Investigator display-label overrides add 0008
# (investigator_label) -> version 4. Widening that override to transactions + flows (transfer/tx_output)
# rebuilds the table's CHECK in 0009 -> version 5. P8.8 clustering widens entity.origin (+ heuristic-cluster)
# and adds erc20_approval in 0010 -> version 6. FN-04 trace edit/retract adds the two append-only
# trace-retraction tables in 0011 -> version 7. FN-17 manual cross-chain bridge link adds
# trace_bridge_link in 0012 -> version 8. FN-15 per-sub-signal risk detail adds risk_detail in 0013 -> version 9.
# P27/FN-19 in-DB append-only audit_baseline anchor adds 0014 -> version 10.
CURRENT_SCHEMA_VERSION = 10


class SchemaTooNewError(Exception):
    """LOG-03: the case DB was created by a NEWER app version — its applied-migration set contains ids
    this build does not ship. Opening it would operate on a schema the app only half-understands, so we
    refuse with a clear message instead of silently proceeding."""


def _sqlite_uri(db_path: Path) -> str:
    # yoyo wants a forward-slash absolute path; on Windows this yields e.g.
    # sqlite:///C:/python/.../case.db which the sqlite backend opens correctly.
    return "sqlite:///" + db_path.resolve().as_posix()


def _init_backend_connection(backend) -> None:
    """Apply project PRAGMAs to yoyo's own connection.

    foreign_keys/busy_timeout are per-connection; yoyo's connection does not set them.
    Setting foreign_keys here (outside any transaction, right after connect) persists across
    the per-migration transactions. Verified: this rejects dangling-FK inserts during a
    migration. Best-effort and guarded so a future yoyo internals change can't break migrate.
    """
    try:
        conn = backend.connection
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
    except Exception:  # pragma: no cover - defensive; migrations are DDL-only regardless
        pass


def apply_migrations(db_path: str | Path) -> int:
    """Apply all pending migrations to ``db_path``. Returns the count applied."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backend = get_backend(_sqlite_uri(path))
    _init_backend_connection(backend)
    migrations = read_migrations(str(MIGRATIONS_DIR))
    try:
        with backend.lock():
            pending = backend.to_apply(migrations)  # a yoyo MigrationList (list subclass)
            count = len(pending)                    # snapshot count before applying
            # Pass the MigrationList itself (NOT list(pending)) — apply_migrations needs its
            # .post_apply attribute, which a plain list lacks.
            backend.apply_migrations(pending)
    finally:
        # Close yoyo's own connection so it doesn't linger as a leaked handle on the case DB. On
        # Windows an open handle locks the file (blocks a later move/delete) and pins the WAL — exactly
        # the leak P4's runtime case-switching must avoid. Best-effort: a backend without a closable
        # connection is left to GC.
        try:
            backend.connection.close()
        except Exception:  # pragma: no cover - defensive across yoyo internals
            pass
    _stamp_schema_version(path)  # LOG-02: keep case_meta.schema_version tracking the applied set
    return count


def _stamp_schema_version(db_path: str | Path) -> None:
    """LOG-02: after a successful apply, stamp ``case_meta.schema_version = CURRENT_SCHEMA_VERSION`` so the
    value tracks the applied migration set. It was previously stamped ONCE at ``init_case`` and never
    updated, so three identically-migrated DBs reported 5/3/1. A fresh DB whose ``case_meta`` row is not
    yet inserted (``init_case`` runs next) is a 0-row no-op; pre-Phase-1 (no ``case_meta`` table) is skipped."""
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE case_meta SET schema_version = ?", (CURRENT_SCHEMA_VERSION,))
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
    finally:
        conn.close()


def known_migration_ids() -> set[str]:
    """The migration ids this app build ships (the app's schema knowledge)."""
    return {m.id for m in read_migrations(str(MIGRATIONS_DIR))}


def applied_migration_ids(db_path: str | Path) -> set[str]:
    """The migration ids recorded as applied in the DB's ``_yoyo_migration`` table (empty for a fresh DB)."""
    conn = get_connection(db_path, create_parents=False)
    try:
        rows = conn.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()  # never migrated yet
        raise
    finally:
        conn.close()


def assert_supported_schema(db_path: str | Path) -> None:
    """LOG-03: refuse to open a case DB created by a NEWER app version. Keys on the ``_yoyo_migration`` set
    (the reliable signal — a pre-LOG-02-fix DB may carry a stale ``schema_version`` stamp), not the stamp.
    If the DB has applied migrations this build doesn't know, raise :class:`SchemaTooNewError`."""
    unknown = applied_migration_ids(db_path) - known_migration_ids()
    if unknown:
        raise SchemaTooNewError(
            "this case was created by a newer version of the app and cannot be opened safely "
            f"(unknown migrations: {', '.join(sorted(unknown))}). Update the app to open it.")


def read_schema_version(db_path: str | Path) -> int:
    """Return ``case_meta.schema_version``, or 0 if the table does not exist yet.

    Pre-Phase-1 there is no ``case_meta`` table; treat *that specific* condition as version 0.
    Any other OperationalError (locked/corrupt DB, I/O error) propagates rather than being
    masked as a clean no-op.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT schema_version FROM case_meta LIMIT 1").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0  # case_meta not created until Phase 1
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m backend.app.db.migrate <db_path>", file=sys.stderr)
        return 2
    db_path = argv[0]
    applied = apply_migrations(db_path)
    version = read_schema_version(db_path)
    print(f"migrate: applied {applied} migration(s) to {db_path}; schema_version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
