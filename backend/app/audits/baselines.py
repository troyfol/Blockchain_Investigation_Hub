"""Cross-run baseline storage for invariant audits (docs/testing.md §2, checks #4 & #6).

Some audits are not single-shot queries — they assert that state has not *regressed* between
runs:

  * **final-immutability** (#4): a checksum of all ``final`` transactions + their children must
    not change once recorded.
  * **append-only claims** (#6): the set of claim ``id``s must only grow.

Such checks need somewhere to persist the previous snapshot. This module provides a tiny
JSON store living in a sidecar directory next to the case DB (default
``<case.db parent>/.audit_baselines/``), so baselines travel with the case and CI can compare
across runs. The store is deliberately dumb (one JSON file per named baseline) — the *policy*
(what to hash, when a change is a failure) lives in each check.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class BaselineStore:
    """Read/write named JSON baselines under a sidecar directory."""

    def __init__(self, baseline_dir: str | Path):
        self.baseline_dir = Path(baseline_dir)

    def _path(self, name: str) -> Path:
        # Names are simple identifiers (check names); keep the filename predictable.
        safe = name.replace("/", "_")
        return self.baseline_dir / f"{safe}.json"

    def read(self, name: str):
        """Return the stored baseline for ``name``, or ``None`` if not yet recorded."""
        path = self._path(name)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, name: str, data) -> None:
        """Persist ``data`` (JSON-serializable) as the baseline for ``name``."""
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        tmp.replace(path)  # atomic on the same filesystem

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def discard(self, name: str) -> bool:
        """Delete the stored baseline for ``name`` — an EXPLICIT operator re-baseline (the runner's
        ``--rebaseline`` flag; review finding BASE-02). The next run of the owning check
        re-establishes it from current state. Returns whether a baseline existed."""
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True


def default_baseline_dir(db_path: str | Path) -> Path:
    """The conventional baseline directory for a given case DB."""
    return Path(db_path).resolve().parent / ".audit_baselines"


# --------------------------------------------------------------------------- in-DB anchor (P27/FN-19)
#
# The JSON sidecar above is tamper-EVIDENCE that lives OUTSIDE the DB — and can therefore be deleted to
# force a silent re-baseline (audits/checks/immutability.py trust model). The `audit_baseline` table
# (migration 0014) commits the baseline INSIDE case.db as an append-only anchor, so a re-opened case whose
# committed state no longer matches the anchor cannot silently re-baseline. This is pure storage; the
# POLICY (what the anchor hashes, when a mismatch is a failure) lives in the owning check — the same
# storage/policy split as BaselineStore. All helpers tolerate a pre-0014 DB (no table) so the owning
# check stays green on an un-migrated/older case DB.


def read_latest_anchor(conn: sqlite3.Connection, baseline_name: str) -> "str | None":
    """The most recent committed ``anchor_hash`` for ``baseline_name`` (append-only history; latest row
    wins), or ``None`` when the table is absent (pre-0014) or no anchor has been recorded yet."""
    try:
        row = conn.execute(
            "SELECT anchor_hash FROM audit_baseline WHERE baseline_name=? ORDER BY id DESC LIMIT 1",
            (baseline_name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table not created yet (older case DB) — anchoring is simply inert
    return row[0] if row else None


def append_anchor(conn: sqlite3.Connection, baseline_name: str, anchor_hash: str, *,
                  row_count: int, schema_version: int, established_at: str) -> bool:
    """Append a superseding anchor for ``baseline_name`` (append-only; the DB triggers refuse UPDATE /
    DELETE). Returns ``False`` as a no-op when the table is absent (pre-0014), so the owning check does
    not crash on an un-migrated DB — e.g. verifying an older imported bundle before it is migrated."""
    try:
        conn.execute(
            "INSERT INTO audit_baseline (baseline_name, anchor_hash, row_count, schema_version, established_at)"
            " VALUES (?,?,?,?,?)",
            (baseline_name, anchor_hash, row_count, schema_version, established_at),
        )
    except sqlite3.OperationalError:
        return False
    return True


def anchor_present(conn: sqlite3.Connection, baseline_name: str) -> bool:
    """Whether ``baseline_name`` has a committed in-DB anchor (used by export to confirm it travels)."""
    return read_latest_anchor(conn, baseline_name) is not None
