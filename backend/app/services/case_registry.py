"""Case registry — a small persisted index of known cases (P4).

A single JSON file in ``app_data_dir()`` records the cases this install has opened: absolute path,
title, a chain summary, ``last_opened`` and ``schema_version``. It drives the entry screen's Recent
list and the launcher's "open the last-active case" startup.

It is an INDEX, not the source of truth: a case's truth is its own ``case.db``. So the registry is
always reconciled against the filesystem on read — entries whose ``case.db`` is gone are pruned (and
the pruned file is rewritten), and ``forget`` removes an entry from the list WITHOUT deleting the case
on disk. Corruption-tolerant: an unreadable/garbage registry reads as empty rather than crashing the
app (the worst case is a forgotten Recent list, never a lost case).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..app_paths import app_data_dir
from ..db.repository import utc_now_iso

REGISTRY_NAME = "cases.json"
REGISTRY_VERSION = 1


def _registry_path() -> Path:
    return app_data_dir() / REGISTRY_NAME


def _norm(path: str | Path) -> str:
    """Canonical absolute string for a case.db path (the registry's natural key)."""
    return str(Path(path).resolve())


def _load() -> dict:
    p = _registry_path()
    if not p.exists():
        return {"version": REGISTRY_VERSION, "cases": []}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
            return {"version": REGISTRY_VERSION, "cases": []}
        return doc
    except (OSError, ValueError):
        # Corrupt/garbage registry -> treat as empty (never crash the app over the Recent list).
        return {"version": REGISTRY_VERSION, "cases": []}


def _save(doc: dict) -> None:
    doc["version"] = REGISTRY_VERSION
    _registry_path().write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")


def _exists(entry: dict) -> bool:
    try:
        return Path(entry.get("path", "")).exists()
    except OSError:
        return False


def list_cases() -> list[dict]:
    """Known cases, most-recently-opened FIRST, pruned of any whose ``case.db`` is gone. The prune is
    persisted, so a moved/deleted case quietly drops off the Recent list on the next read."""
    doc = _load()
    live = [e for e in doc["cases"] if _exists(e)]
    if len(live) != len(doc["cases"]):
        doc["cases"] = live
        _save(doc)
    return sorted(live, key=lambda e: e.get("last_opened") or "", reverse=True)


def register(path: str | Path, *, title: str | None = None, chains: list[str] | None = None,
             schema_version: int | None = None, trusted: bool = True,
             last_opened: str | None = None) -> dict:
    """Upsert a case by its absolute ``case.db`` path and stamp ``last_opened`` (so it surfaces atop
    the Recent list). Returns the stored entry. Re-registering an existing case updates its metadata
    in place — never duplicates (idempotent, like the rest of the project)."""
    key = _norm(path)
    doc = _load()
    ts = last_opened or utc_now_iso()
    existing = next((e for e in doc["cases"] if _norm(e.get("path", "")) == key), None)
    entry = existing or {"path": key}
    entry["path"] = key
    if title is not None:
        entry["title"] = title
    if chains is not None:
        entry["chains"] = list(chains)
    if schema_version is not None:
        entry["schema_version"] = schema_version
    entry["trusted"] = bool(trusted)
    entry["last_opened"] = ts
    if existing is None:
        doc["cases"].append(entry)
    _save(doc)
    return entry


def ensure(path: str | Path, **meta) -> bool:
    """Register a case ONLY if it isn't already known — so the live active case appears in the Recent
    list without churning its ``last_opened`` on every read. Returns whether it was newly added."""
    key = _norm(path)
    if any(_norm(e.get("path", "")) == key for e in _load()["cases"]):
        return False
    register(path, **meta)
    return True


def forget(path: str | Path) -> bool:
    """Remove a case from the Recent list WITHOUT deleting it on disk. Returns whether anything was
    removed. (The case.db is untouched — 'remove from list' is not 'delete the case'.)"""
    key = _norm(path)
    doc = _load()
    before = len(doc["cases"])
    doc["cases"] = [e for e in doc["cases"] if _norm(e.get("path", "")) != key]
    removed = len(doc["cases"]) != before
    if removed:
        _save(doc)
    return removed


def last_opened_path() -> str | None:
    """The most-recently-opened case that still exists on disk, or ``None`` (launcher startup)."""
    cases = list_cases()
    return cases[0]["path"] if cases else None
