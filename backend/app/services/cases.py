"""Active-case lifecycle + case management (P4).

This is the runtime "which case am I looking at" service. The active case is a MUTABLE in-process
value the picker sets at runtime (replacing the old static ``BIH_CASE_DB`` env read in ``main.py``).
Connections stay per-request (``db/connection.py``); this module never holds a long-lived handle. When
the active case SWITCHES, the prior case's WAL is checkpointed+truncated so nothing leaks (no orphan
``-wal`` growing under a case nobody is viewing).

Path resolution is funnelled through ``app_paths`` (one helper P7 repoints at the frozen-app user
dir). Opening or importing a case READS it — it never executes anything from the bundle (data, not
commands). Import always verifies the bundle FIRST and only opens it if verification passes (or the
caller explicitly accepts an untrusted bundle).

Startup default (``active_case_path``): an explicit runtime switch wins; else ``BIH_CASE_DB`` (dev /
``make run``); else the registry's last-opened case; else ``None`` -> the entry screen (empty state).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
from pathlib import Path

from ..db import apply_migrations, assert_supported_schema, get_connection
from ..db import repository as repo
from ..services.export import verify_casefile
from . import case_registry
from .settings_store import cases_root as _cases_root

# The active case: an absolute case.db path, or None (no case -> picker). Guarded because uvicorn may
# service requests on different worker threads; the value is small and writes are rare (only switches).
_active_path: str | None = None
_lock = threading.RLock()

# The live pywebview Window (windowed mode only) the native file dialog runs against. None in dev/CI.
_native_window = None


def _norm(path: str | Path) -> str:
    return str(Path(path).resolve())


# --------------------------------------------------------------------------- active case

def active_case_path() -> str | None:
    """The active case.db path, or ``None`` if no case is active (entry screen). Resolution order:
    an explicit runtime switch -> ``BIH_CASE_DB`` env -> the registry's last-opened -> None."""
    with _lock:
        if _active_path is not None:
            return _active_path
    env = os.environ.get("BIH_CASE_DB")
    if env:
        return env
    return case_registry.last_opened_path()


def clear_active_case() -> None:
    """Reset the in-process active case (test isolation; also a future 'close case' hook)."""
    from . import jobs

    global _active_path
    jobs.cancel_active()  # stop any background fetch/valuation bound to the (about-to-be-cleared) case
    with _lock:
        _active_path = None


def _checkpoint_release(path: str | Path) -> None:
    """Flush+truncate a case's WAL so switching away leaves no orphaned ``-wal``/``-shm`` growing under
    a case nobody is viewing. Best-effort (a locked/missing DB is left as-is) and mirrors the same
    checkpoint export does before bundling."""
    db = Path(path)
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def set_active_case(path: str | Path, *, migrate: bool = True, trusted: bool = True) -> dict:
    """Make ``path`` the active case: checkpoint+release the PRIOR case's WAL, migrate this one forward
    (idempotent), record it in the registry, and switch. Returns ``{path, migrated, applied}`` so the
    caller can surface whether a migration ran."""
    from . import jobs

    global _active_path
    norm = _norm(path)
    if migrate:
        # LOG-03: refuse a case DB created by a NEWER app version BEFORE migrating/stamping it (a forward
        # migration can't undo an unknown one; keying on the applied-migration set is the reliable signal).
        assert_supported_schema(norm)
    with _lock:
        prior = _active_path
        if prior and _norm(prior) != norm:
            # P8.7.2 — stop any background job (fetch/valuation) on the PRIOR case before checkpointing it,
            # so it can't keep writing into a case the user just left (and can't fight the WAL checkpoint).
            jobs.cancel_active()
            _checkpoint_release(prior)
        applied = apply_migrations(norm) if migrate else 0
        _active_path = norm
        # RES-03: clear any `raw_responses/*.tmp` stragglers a prior hard crash left staged-but-uncommitted.
        try:
            from ..provenance.atomic import sweep_stale_raw_tmp
            sweep_stale_raw_tmp(Path(norm).parent)
        except Exception:  # never block a case switch on best-effort hygiene
            pass
    summary = _read_case_summary(norm)
    case_registry.register(norm, title=summary.get("title"), chains=summary.get("chains"),
                           schema_version=summary.get("schema_version"), trusted=trusted)
    return {"path": norm, "migrated": applied > 0, "applied": applied}


# --------------------------------------------------------------------------- case metadata

def _read_case_summary(path: str | Path) -> dict:
    """(title, chains, schema_version) for a case.db, or ``{}`` if it isn't a readable BIH case."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        conn = get_connection(p, create_parents=False)
    except sqlite3.Error:
        return {}
    try:
        row = conn.execute("SELECT title, schema_version FROM case_meta LIMIT 1").fetchone()
        if row is None:
            return {}
        chains = [r[0] for r in conn.execute(
            "SELECT DISTINCT chain FROM address ORDER BY chain").fetchall()]
        return {"title": row["title"], "schema_version": row["schema_version"], "chains": chains}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def active_meta() -> dict | None:
    """Full metadata for the active case (drives the app header/title + Recent badge), or ``None`` if
    no case is active or the active path isn't a readable BIH case. Idempotently ensures the active
    case appears in the Recent list."""
    path = active_case_path()
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        conn = get_connection(p, create_parents=False)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id, title, description, status, schema_version, created_at, updated_at "
            "FROM case_meta LIMIT 1").fetchone()
        if row is None:
            return None
        chains = [r[0] for r in conn.execute(
            "SELECT DISTINCT chain FROM address ORDER BY chain").fetchall()]
        n_addr = conn.execute("SELECT COUNT(*) FROM address").fetchone()[0]
        n_tx = conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    meta = {
        "path": _norm(p), "title": row["title"], "description": row["description"],
        "status": row["status"], "schema_version": row["schema_version"], "chains": chains,
        "address_count": n_addr, "tx_count": n_tx,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    # Surface the live active case in Recent without churning its last_opened on every read.
    try:
        case_registry.ensure(p, title=row["title"], chains=chains,
                             schema_version=row["schema_version"])
    except OSError:
        pass
    return meta


# --------------------------------------------------------------------------- new / open / import

_WIN_RESERVED_STEM = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-") or "case"
    # SEC-10: a Windows reserved device name ('CON'/'NUL'/'COM1'…) as a whole folder name misbehaves;
    # prefix it. Cap the component length (NTFS 255 / default MAX_PATH 260 headroom).
    if _WIN_RESERVED_STEM.match(s):
        s = f"case-{s}"
    return s[:64]


def _confined_root(location: str | Path | None, *, param: str) -> Path:
    """Resolve a caller-supplied output base and REQUIRE it to live inside the cases root (SEC-06).

    A ``None`` location falls back to the cases root. An absolute/relative path that resolves outside
    ``cases_root()`` is rejected (``ValueError`` → HTTP 400) so a same-origin script can't create a case
    folder / extract a bundle at an arbitrary process-writable path (e.g. a Windows Startup dir). A user
    who wants cases elsewhere changes the cases folder via settings (``set_cases_root``), not per-request."""
    root = _cases_root().resolve()
    if location is None:
        return root
    target = Path(location)
    target = (target if target.is_absolute() else root / target).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{param} must be inside the cases root ({root})")
    return target


def _unique_dir(base: Path) -> Path:
    """``base`` if free, else ``base-2``, ``base-3``… so a new case never clobbers an existing folder."""
    if not base.exists():
        return base
    i = 2
    while (base.parent / f"{base.name}-{i}").exists():
        i += 1
    return base.parent / f"{base.name}-{i}"


def new_case(title: str, *, location: str | Path | None = None) -> dict:
    """Create a fresh case folder (at ``location`` or the default cases root), migrate it, write its
    ``case_meta``, register it, and make it active. Returns ``{path, created: True}``."""
    title = (title or "").strip()
    if not title:
        raise ValueError("a case needs a non-empty title")
    root = _confined_root(location, param="location")
    case_dir = _unique_dir(root / _slug(title))
    case_db = case_dir / "case.db"
    apply_migrations(case_db)  # creates parents + schema
    conn = get_connection(case_db)
    try:
        if conn.execute("SELECT 1 FROM case_meta LIMIT 1").fetchone() is None:
            repo.init_case(conn, title=title)
    finally:
        conn.close()
    res = set_active_case(case_db)
    return {"path": res["path"], "created": True}


def open_case(path: str | Path) -> dict:
    """Open an existing case (a ``case.db`` or a case folder). Validates it is a BIH case (has
    ``case_meta``), migrates it forward, registers it, and makes it active. Returns
    ``{path, migrated, applied}``."""
    p = Path(path)
    if p.is_dir():
        p = p / "case.db"
    if not p.exists():
        raise FileNotFoundError(f"no case.db found at {p}")
    conn = get_connection(p, create_parents=False)
    try:
        try:
            is_case = conn.execute("SELECT 1 FROM case_meta LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            is_case = False
    finally:
        conn.close()
    if not is_case:
        raise ValueError(f"{p} is not a Blockchain Investigation Hub case (no case_meta table)")
    return set_active_case(p)


def import_casefile(casefile_path: str | Path, *, allow_untrusted: bool = False,
                    dest_root: str | Path | None = None) -> dict:
    """Extract + VERIFY a ``.casefile`` before opening it. Verification is the gate: a bundle that
    fails (hash mismatch, path-escape, not self-contained) is NOT opened unless ``allow_untrusted`` is
    explicitly set (the loud 'open anyway' path). Importing reads data only — nothing in the bundle is
    executed. Returns ``{verification, opened, trusted, [path, migrated]}``."""
    # Confinement is checked FIRST (before the file is read) so a hostile dest_root is rejected up front.
    root = _confined_root(dest_root, param="dest_root")
    casefile = Path(casefile_path)
    if not casefile.exists():
        raise FileNotFoundError(f"no .casefile at {casefile}")
    dest = _unique_dir(root / (casefile.stem or "imported-case"))
    result = verify_casefile(casefile, extract_to=dest)
    out: dict = {"verification": result, "opened": False, "trusted": bool(result["ok"]),
                 "extracted_to": str(dest)}
    if result["ok"] or allow_untrusted:
        case_db = dest / "case.db"
        res = set_active_case(case_db, trusted=result["ok"])
        out.update(opened=True, migrated=res["migrated"], path=res["path"])
    else:
        # Verification failed and the caller did not accept an untrusted bundle: do not open, and do
        # not leave an untracked extracted copy lying around.
        shutil.rmtree(dest, ignore_errors=True)
        out["extracted_to"] = None
    return out


# --------------------------------------------------------------------------- native window (dialogs)

def register_native_window(window) -> None:
    """The launcher registers its pywebview Window so the native file dialog can run against it. Dev /
    browser mode never registers one (the frontend uses the HTML upload + path field fallback)."""
    global _native_window
    _native_window = window


def get_native_window():
    return _native_window
