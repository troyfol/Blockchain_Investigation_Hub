"""Findings, annotations, and tags (phase_08).

Investigator-authored objects with polymorphic, app-enforced references to any object in the case.
References are validated on write (the target row must exist and its declared type must be allowed)
so the no-dangling-fk audit (docs/testing.md §2 #2) stays green. Tags are deliberately separate from
source attributions — an investigator's label is not a source's claim.
"""

from __future__ import annotations

from ..db import repository as repo
from ..models import Annotation, Finding, FindingRef, InvestigatorLabel, Tag
from ..models.investigator import (
    ANNOTATION_TARGET_TYPES,
    FINDING_REF_TYPES,
    INVESTIGATOR_LABEL_TARGET_TYPES,
    TAG_TARGET_TYPES,
)

# poly-ref type -> the table that holds the id. This is the UNION of every object's allowed types
# (used only for the existence check); each add_* first restricts ref/target_type to its own allowed
# set (FINDING_REF_TYPES / ANNOTATION_TARGET_TYPES / TAG_TARGET_TYPES) before reaching _require_target.
_TYPE_TABLE = {
    "address": "address", "transfer": "transfer", "transaction": "transaction_",
    "tx_output": "tx_output", "trace": "trace", "exhibit": "exhibit", "entity": "entity",
    "finding": "finding",
}


def _require_target(conn, ref_type: str, ref_id: str) -> None:
    table = _TYPE_TABLE[ref_type]
    if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (ref_id,)).fetchone() is None:
        raise ValueError(f"{ref_type} {ref_id!r} not found (poly ref would dangle)")


def create_finding(conn, *, statement: str, assessment: str | None = None,
                   now: str | None = None) -> str:
    return repo.insert_finding(conn, Finding(statement=statement, assessment=assessment), now=now)


def add_finding_ref(conn, *, finding_id: str, ref_type: str, ref_id: str,
                    note: str | None = None) -> str:
    if conn.execute("SELECT 1 FROM finding WHERE id=?", (finding_id,)).fetchone() is None:
        raise ValueError(f"finding {finding_id!r} not found")
    if ref_type not in FINDING_REF_TYPES:
        raise ValueError(f"invalid finding ref_type {ref_type!r}")
    _require_target(conn, ref_type, ref_id)
    return repo.insert_finding_ref(conn, FindingRef(
        finding_id=finding_id, ref_type=ref_type, ref_id=ref_id, note=note))


def add_annotation(conn, *, target_type: str, target_id: str, content: str,
                   now: str | None = None) -> str:
    if target_type not in ANNOTATION_TARGET_TYPES:
        raise ValueError(f"invalid annotation target_type {target_type!r}")
    _require_target(conn, target_type, target_id)
    return repo.insert_annotation(conn, Annotation(
        target_type=target_type, target_id=target_id, content=content), now=now)


def _annotation_target(conn, annotation_id: str) -> tuple[str, str]:
    """(target_type, target_id) for an annotation — so an edit/delete can return the refreshed list and
    the caller can re-render the green outline/glow. Raises if the annotation does not exist."""
    row = conn.execute(
        "SELECT target_type, target_id FROM annotation WHERE id=?", (annotation_id,)).fetchone()
    if row is None:
        raise ValueError(f"annotation {annotation_id!r} not found")
    return row["target_type"], row["target_id"]


def update_annotation(conn, *, annotation_id: str, content: str) -> tuple[str, str]:
    """Edit an annotation in place (Family C investigator input, like a finding — editable until
    reported). Returns the annotation's (target_type, target_id)."""
    tt, tid = _annotation_target(conn, annotation_id)
    content = (content or "").strip()
    if not content:
        raise ValueError("annotation content must be non-empty")
    repo.update_annotation(conn, annotation_id, content=content)
    return tt, tid


def delete_annotation(conn, *, annotation_id: str) -> tuple[str, str]:
    """Delete an annotation. Returns its (target_type, target_id) so the caller can refresh the target's
    note list + the green outline/glow (which clears when the last note on a target is removed)."""
    tt, tid = _annotation_target(conn, annotation_id)
    repo.delete_annotation(conn, annotation_id)
    return tt, tid


def add_tag(conn, *, target_type: str, target_id: str, label: str, now: str | None = None) -> str:
    if target_type not in TAG_TARGET_TYPES:
        raise ValueError(f"invalid tag target_type {target_type!r}")
    _require_target(conn, target_type, target_id)
    return repo.insert_tag(conn, Tag(target_type=target_type, target_id=target_id, label=label), now=now)


def set_label(conn, *, target_type: str, target_id: str, label: str, now: str | None = None) -> str:
    """Set an investigator display-label override on a node (address) or a trace/path.

    A CLAIM (the investigator's own label), never a fact: it takes display precedence on the graph and
    in the report but leaves the underlying address/facts untouched (Invariants #5/#6). Append-only —
    the most-recent label for the target is the one shown; the prior labels remain as history.
    """
    label = (label or "").strip()
    if not label:
        raise ValueError("label must be a non-empty string")
    if target_type not in INVESTIGATOR_LABEL_TARGET_TYPES:
        raise ValueError(f"invalid label target_type {target_type!r}")
    _require_target(conn, target_type, target_id)
    return repo.insert_investigator_label(
        conn, InvestigatorLabel(target_type=target_type, target_id=target_id, label=label), now=now)


def current_labels(conn, target_type: str) -> dict[str, str]:
    """The current display-label override per target (latest wins) — used by the read-model/report."""
    return repo.current_investigator_labels(conn, target_type)


# --- finding edit/delete (editable until reported; not a sourced claim) ----------------------

def update_finding(conn, *, finding_id: str, statement: str, assessment: str | None = None) -> None:
    if conn.execute("SELECT 1 FROM finding WHERE id=?", (finding_id,)).fetchone() is None:
        raise ValueError(f"finding {finding_id!r} not found")
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("finding statement must be non-empty")
    repo.update_finding(conn, finding_id, statement=statement, assessment=assessment)


def delete_finding(conn, *, finding_id: str) -> None:
    if conn.execute("SELECT 1 FROM finding WHERE id=?", (finding_id,)).fetchone() is None:
        raise ValueError(f"finding {finding_id!r} not found")
    repo.delete_finding(conn, finding_id)


def remove_finding_ref(conn, *, ref_id: str) -> None:
    repo.delete_finding_ref(conn, ref_id)


# --- read helpers for the side panel + the Findings & Notes composer -------------------------

def _display(conn, ttype: str, tid: str) -> tuple[str, str | None]:
    """(display label, graph node id) for a target/ref — for jump-to-node + readable lists."""
    from .graph import _alias  # local import avoids a cycle (graph imports nothing from here)

    if ttype == "address":
        r = conn.execute("SELECT address, address_display FROM address WHERE id=?", (tid,)).fetchone()
        return (_alias(r["address_display"] or r["address"]) if r else tid), f"addr:{tid}"
    if ttype == "transaction":
        r = conn.execute("SELECT tx_hash FROM transaction_ WHERE id=?", (tid,)).fetchone()
        return (_alias(r["tx_hash"]) if r else tid), f"tx:{tid}"
    if ttype == "tx_output":
        return f"output {_alias(tid)}", None
    if ttype == "transfer":
        return f"transfer {_alias(tid)}", None
    if ttype == "trace":
        r = conn.execute("SELECT name FROM trace WHERE id=?", (tid,)).fetchone()
        return (r["name"] if r else tid), None
    if ttype == "entity":
        r = conn.execute("SELECT name FROM entity WHERE id=?", (tid,)).fetchone()
        return (r["name"] if r and r["name"] else f"entity {_alias(tid)}"), None
    return tid, None


def list_annotations(conn, *, target_type: str, target_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, content, created_at FROM annotation WHERE target_type=? AND target_id=? "
        "ORDER BY created_at, rowid", (target_type, target_id)).fetchall()]


def list_findings(conn) -> list[dict]:
    """Findings + their refs, enriched with a display label + jump-to node id per ref."""
    out: list[dict] = []
    for f in conn.execute(
        "SELECT id, statement, assessment, created_at FROM finding ORDER BY created_at, id").fetchall():
        refs = []
        for r in conn.execute(
            "SELECT id, ref_type, ref_id, note FROM finding_ref WHERE finding_id=? ORDER BY id",
            (f["id"],)).fetchall():
            label, node_id = _display(conn, r["ref_type"], r["ref_id"])
            refs.append({"id": r["id"], "ref_type": r["ref_type"], "ref_id": r["ref_id"],
                         "note": r["note"], "label": label, "node_id": node_id})
        out.append({"id": f["id"], "statement": f["statement"], "assessment": f["assessment"],
                    "created_at": f["created_at"], "refs": refs})
    return out


def collect_notes(conn) -> list[dict]:
    """Every investigator INPUT in the case — annotations, display-label overrides, tags — grouped by
    target (with a jump-to node id where the target is a graph node). The Findings & Notes panel source."""
    groups: dict[tuple[str, str], dict] = {}

    def grp(ttype: str, tid: str) -> dict:
        key = (ttype, tid)
        if key not in groups:
            groups[key] = {"target_type": ttype, "target_id": tid, "node_id": None, "label": None,
                           "annotations": [], "label_override": None, "tags": []}
        return groups[key]

    for r in conn.execute(
        "SELECT id, target_type, target_id, content, created_at FROM annotation "
        "ORDER BY created_at, rowid").fetchall():
        grp(r["target_type"], r["target_id"])["annotations"].append(
            {"id": r["id"], "content": r["content"], "created_at": r["created_at"]})
    for r in conn.execute(
        "SELECT target_type, target_id, label FROM investigator_label ORDER BY created_at, rowid").fetchall():
        grp(r["target_type"], r["target_id"])["label_override"] = r["label"]  # latest wins
    for r in conn.execute(
        "SELECT target_type, target_id, label FROM tag ORDER BY created_at, rowid").fetchall():
        grp(r["target_type"], r["target_id"])["tags"].append(r["label"])

    for (ttype, tid), g in groups.items():
        label, node_id = _display(conn, ttype, tid)
        g["node_id"] = node_id
        g["label"] = g["label_override"] or label
    return sorted(groups.values(), key=lambda x: (x["target_type"], x["label"] or ""))
