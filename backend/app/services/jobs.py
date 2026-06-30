"""In-process long-operation jobs — progress + cooperative cancel (P8.7.2).

The app is single-user/local, so there is at most ONE long operation in flight (a fetch/ingest or a
valuation pass). This module holds a single ACTIVE job that:

  * the connector base updates as it works — ``note_request`` per fetched page / price call (so a busy
    ingest reports "N pages" and a valuation reports progress), ``note_backoff`` on a 429/5xx backoff
    (so the UI can show "rate-limited — backing off");
  * the caller polls (``GET /api/jobs/active``) for a live progress line;
  * the caller can CANCEL (``POST /api/jobs/cancel``) — which sets a flag the worker checks between pages
    (``check_cancel`` raises :class:`JobCancelled`). Cancel is COOPERATIVE + checked only at page
    boundaries, BEFORE a ``write_with_provenance`` (which is atomic), so a canceled run never leaves a
    partial/orphaned write — it stops cleanly between complete source_queries.

Starting a new job SUPERSEDES (cancels) any still-running one, so a fresh ingest stops a background
valuation. A finished/canceled/errored job stays readable until the next ``start`` replaces it.
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar


class JobCancelled(Exception):
    """Raised by ``check_cancel`` when the active job was canceled — propagates out of the worker
    (connector/valuation) so the request handler can return a clean 'canceled' result."""


class Job:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind                  # 'ingest' | 'valuation'
        self.state = "running"            # running | done | canceled | error
        self.phase = ""                   # 'fetching' | 'valuing' | ...
        self.requests = 0                 # pages fetched / price calls made
        self.valued = 0                   # movements valued so far (valuation)
        self.total = 0                    # movements to value (valuation) — 0 when unknown
        self.rate_limited = False         # currently backing off a 429/5xx
        self.message = ""
        self.error: str | None = None
        self.result: dict | None = None
        self._cancel = threading.Event()

    # -- worker-side --------------------------------------------------------------------------
    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def finish(self, result: dict | None = None) -> None:
        self.result = result or {}
        self.message = ""
        if self.state == "running":
            self.state = "done"

    def fail(self, error: str) -> None:
        self.error = error
        if self.state == "running":
            self.state = "error"

    def mark_canceled(self) -> None:
        self._cancel.set()
        self.state = "canceled"

    # -- control / read -----------------------------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()
        if self.state == "running":
            self.state = "canceled"

    def status(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "phase": self.phase,
            "requests": self.requests, "valued": self.valued, "total": self.total,
            "rate_limited": self.rate_limited, "message": self.message, "error": self.error,
        }


_lock = threading.RLock()
_active: Job | None = None

# The job the CURRENT worker (thread/context) owns. The connector hooks operate on THIS, not the global
# active job — so a SUPERSEDED worker (e.g. a dying background valuation) can only touch its own (already
# canceled) job, never the new one's progress, and a connector call OUTSIDE any job (e.g. a Chainalysis
# screen during "Check intel") never bumps/cancels an unrelated active job. Each worker binds it: the
# request thread via ``start`` (same thread runs the fetch); the background valuation thread via ``bind``.
_current: ContextVar["Job | None"] = ContextVar("bih_current_job", default=None)


def start(kind: str) -> Job:
    """Begin a new active job, SUPERSEDING (canceling) any still-running one, and bind it to THIS context."""
    global _active
    with _lock:
        if _active is not None and _active.state == "running":
            _active.cancel()  # a fresh operation stops the prior one (e.g. ingest stops bg valuation)
        _active = Job(kind)
    _current.set(_active)
    return _active


def bind(job: Job) -> None:
    """Bind ``job`` as THIS context's worker job (the background valuation thread calls this so its
    connector calls report to its own job, not whatever became globally active meanwhile)."""
    _current.set(job)


def active() -> Job | None:
    with _lock:
        return _active


def cancel_active() -> bool:
    """Cancel the active job if it's running. Returns whether a running job was canceled."""
    with _lock:
        if _active is not None and _active.state == "running":
            _active.cancel()
            return True
        return False


def clear() -> None:
    """Drop the active job + this context's bound job (test isolation)."""
    global _active
    with _lock:
        _active = None
    _current.set(None)


# --------------------------------------------------------------------------- worker hooks (connector base)
# These operate on THIS context's bound job (``_current``), NOT the global active job — see the comment on
# ``_current``. A no-op when the current worker has no job bound (a connector call outside any operation).

def note_request() -> None:
    """A successful fetched page / price call: bump progress, clear the backoff flag, and honor cancel.
    The cancel check runs REGARDLESS of state (a cancel flips state to 'canceled' yet must still raise)."""
    j = _current.get()
    if j is None:
        return
    if j.state == "running":
        j.requests += 1
        j.rate_limited = False
    j.check_cancel()


def note_backoff() -> None:
    """A 429/5xx/transport backoff: flag rate-limited (UI shows it) and honor cancel even mid-backoff."""
    j = _current.get()
    if j is None:
        return
    if j.state == "running":
        j.rate_limited = True
    j.check_cancel()


def check_cancel() -> None:
    j = _current.get()
    if j is not None:
        j.check_cancel()
