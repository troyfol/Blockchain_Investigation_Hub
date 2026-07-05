"""Exhibits (Phase 7 step 4): screenshot-as-exhibit fallback for visually-only data.

When a tool surfaces data only visually (no export), attach a screenshot as a hashed ``exhibit``
(type='screenshot') stored under ``<case>/exhibits/``. Exhibits are investigator artifacts (no
``source_query`` — they are not API/import claims), but they ARE content-hashed for tamper-evidence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from ..db.repository import utc_now_iso

EXHIBITS_SUBDIR = "exhibits"


def _case_dir(conn) -> Path | None:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file).parent if file else None
    return None


def attach_screenshot(conn, *, file_path, source: str | None = None, description: str | None = None,
                      captured_at: str | None = None) -> str:
    """Store a screenshot file as a hashed exhibit. Returns the exhibit id."""
    raw = Path(file_path).read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    case_dir = _case_dir(conn)
    name = Path(file_path).name
    file_ref = f"{EXHIBITS_SUBDIR}/{name}"
    if case_dir is not None:
        dest = case_dir / EXHIBITS_SUBDIR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    exhibit_id = str(uuid4())
    conn.execute(
        "INSERT INTO exhibit (id, exhibit_type, source, captured_at, file_ref, content_hash, description) "
        "VALUES (?,?,?,?,?,?,?)",
        (exhibit_id, "screenshot", source, captured_at or utc_now_iso(), file_ref, content_hash, description))
    return exhibit_id


def numbered_exhibits(conn) -> list[dict]:
    """Every exhibit with a STABLE 1-based number (FN-10), assigned by a DETERMINISTIC sort
    (``captured_at``, ``id`` — both immutable once written). The same case therefore always yields the same
    exhibit numbers across report renders (report immutability): numbering never depends on insertion order
    or a clock. Each dict carries the exhibit columns plus ``number`` and ``label`` (``"Exhibit N"``) — the
    List-of-Exhibits source and the citation label findings cross-reference."""
    rows = conn.execute(
        "SELECT id, exhibit_type, source, captured_at, file_ref, content_hash, description "
        "FROM exhibit ORDER BY captured_at, id").fetchall()
    out = []
    for i, r in enumerate(rows, start=1):
        d = dict(r)
        d["number"] = i
        d["label"] = f"Exhibit {i}"
        out.append(d)
    return out
