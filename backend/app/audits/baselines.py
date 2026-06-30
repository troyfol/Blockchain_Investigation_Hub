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


def default_baseline_dir(db_path: str | Path) -> Path:
    """The conventional baseline directory for a given case DB."""
    return Path(db_path).resolve().parent / ".audit_baselines"
