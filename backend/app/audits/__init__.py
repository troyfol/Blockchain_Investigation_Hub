"""Invariant audit framework (docs/testing.md §2).

Audits are runnable checks that encode the CLAUDE.md §1 Invariants as queries that FAIL
LOUDLY. Each check is a function decorated with ``@audit_check("name")`` that takes an
:class:`AuditContext` and returns an :class:`AuditResult`. The runner (``runner.py``)
discovers every decorated check under ``audits.checks`` and runs them.

The context carries the open connection plus a :class:`~backend.app.audits.baselines.BaselineStore`
so that *cross-run* checks (final-immutability checksum, append-only-claims snapshot — Phase 1
checks #4 and #6 in docs/testing.md §2) can persist and compare state between runs without
each check reinventing storage.

Phase 0 ships the framework with **zero checks** — ``make audit`` is a green no-op.
Phase 1+ add the concrete invariant checks into ``audits/checks/``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle at module load
    from .baselines import BaselineStore

CHECK_MARKER = "_bih_audit_check"
CHECK_NAME = "_bih_audit_name"


@dataclass
class AuditContext:
    """Everything a check needs to run.

    Passing a context (rather than a bare connection) keeps the check signature stable as
    new needs appear — Phase 1 checks read ``ctx.conn``; cross-run checks use
    ``ctx.baselines``.
    """

    conn: sqlite3.Connection
    db_path: Path
    baselines: "BaselineStore"


@dataclass
class AuditResult:
    """Outcome of a single invariant check."""

    name: str
    passed: bool
    offending: list = field(default_factory=list)
    detail: str = ""

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def audit_check(name: str):
    """Mark a function ``fn(ctx: AuditContext) -> AuditResult`` as a discoverable check."""

    def decorator(fn):
        setattr(fn, CHECK_MARKER, True)
        setattr(fn, CHECK_NAME, name)
        return fn

    return decorator


__all__ = [
    "AuditContext",
    "AuditResult",
    "audit_check",
    "CHECK_MARKER",
    "CHECK_NAME",
]
