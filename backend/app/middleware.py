"""Request-provenance middleware (R6 Batch 2 — SEC-02/SEC-04).

The Hub is single-user + local (Invariant #2): the API binds ``127.0.0.1`` with no auth of any kind, so
its only defense against a hostile web page in the user's browser (threat surface d) is *request
provenance*:

- **Host allowlist (every request) — blocks DNS rebinding (SEC-02).** A rebinding page resolves its own
  domain to ``127.0.0.1`` and reaches the loopback socket, but the browser still puts the *attacker's*
  hostname in ``Host``. Requiring ``Host`` to be a loopback authority rejects it even though the TCP peer
  is 127.0.0.1.
- **Origin/Referer check on state-changing methods — blocks cross-site CSRF (SEC-04).** An empty-body /
  simple-content-type POST (``/api/valuation/run``, ``/api/jobs/cancel``, the raw-body ``import-upload``…)
  can be fired cross-site with no preflight; if it carries a foreign ``Origin`` (or ``Referer``) it is
  rejected. A same-origin or absent Origin (the real UI, the launcher's own health probe, non-browser
  clients) passes.

This is defense-in-depth, not a replacement for the never-a-500 error boundary (Batch 5).
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from starlette.datastructures import Headers

# Loopback hostnames a legitimate request can carry. The bound port is OS-assigned (127.0.0.1:0), so the
# port is NOT constrained — only the hostname. ``testserver`` is Starlette's in-process TestClient
# authority: it is synthetic (no DNS record, unreachable by any browser or network peer), so allowing it
# lets the ASGI test harness run without weakening the rebinding defense for real traffic.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})

# Methods that mutate state (CSRF-relevant). Safe methods (GET/HEAD/OPTIONS) get only the Host check.
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _authority_host(authority: str) -> str:
    """Bare hostname of a ``Host`` header value (strip the port; unwrap ``[::1]``)."""
    a = authority.strip()
    if not a:
        return ""
    if a.startswith("["):  # bracketed IPv6, e.g. [::1]:5173
        return a[1:a.index("]")].lower() if "]" in a else a[1:].lower()
    return (a.rsplit(":", 1)[0] if ":" in a else a).lower()


def _is_loopback_host(host_header: str) -> bool:
    return _authority_host(host_header) in _LOOPBACK_HOSTS


def _is_loopback_origin(origin_or_referer: str) -> bool:
    """True iff an ``Origin`` / ``Referer`` URL's host is loopback. A ``null`` / hostless origin is not."""
    host = urlsplit(origin_or_referer).hostname
    return bool(host) and host.lower() in _LOOPBACK_HOSTS


class RequestProvenanceMiddleware:
    """Pure-ASGI middleware (no BaseHTTPMiddleware body buffering) enforcing the Host + Origin checks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not _is_loopback_host(headers.get("host", "")):
            await _reject(send, "request Host is not a loopback address")
            return
        if scope["method"] in _STATE_CHANGING:
            origin = headers.get("origin")
            referer = headers.get("referer")
            if origin is not None:
                if not _is_loopback_origin(origin):
                    await _reject(send, "cross-origin request rejected")
                    return
            elif referer is not None and not _is_loopback_origin(referer):
                await _reject(send, "cross-site request rejected")
                return
        await self.app(scope, receive, send)


async def _reject(send, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body})
