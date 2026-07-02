"""Batch 2 (SEC-02 / SEC-04): the localhost API must reject requests whose provenance is not loopback.

The API binds ``127.0.0.1`` with no auth (single-user, local — Invariant #2), so its only defense against
a hostile web page is request-provenance: a ``Host`` header that is not a loopback address means a
DNS-rebinding attempt (SEC-02), and a state-changing request carrying a foreign ``Origin`` is cross-site
CSRF (SEC-04). A same-origin / no-Origin loopback request must still succeed (the real UI must not break).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_rebinding_host_is_rejected():
    # A DNS-rebinding page reaches 127.0.0.1 but carries its own hostname in Host.
    r = client.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 403, "a non-loopback Host must be rejected (DNS-rebinding defense)"


def test_loopback_host_is_allowed():
    for host in ("127.0.0.1:8000", "localhost", "[::1]:5173"):
        r = client.get("/health", headers={"Host": host})
        assert r.status_code == 200, f"a loopback Host ({host}) must pass"


def test_cross_origin_state_change_is_rejected():
    # An empty-body POST from a hostile page (foreign Origin) must not fire.
    r = client.post("/api/jobs/cancel", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403, "a cross-origin state-changing POST must be rejected (CSRF defense)"


def test_cross_origin_import_upload_is_rejected():
    r = client.post("/api/cases/import-upload?filename=evil.casefile&allow_untrusted=true",
                    headers={"Origin": "http://evil.example"}, content=b"not a real bundle")
    assert r.status_code == 403, "cross-origin import-upload (allow_untrusted) must be rejected"


def test_same_origin_state_change_is_allowed():
    # The real UI POSTs same-origin; this must NOT be blocked (it may still 4xx/2xx on its own merits,
    # just never 403 from the provenance guard).
    r = client.post("/api/jobs/cancel", headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code != 403, "a same-origin POST must not be blocked by the provenance guard"


def test_no_origin_state_change_is_allowed():
    # A no-Origin POST (same-origin navigation / non-browser client like the launcher) must pass.
    r = client.post("/api/jobs/cancel")
    assert r.status_code != 403, "a no-Origin POST must not be blocked by the provenance guard"
