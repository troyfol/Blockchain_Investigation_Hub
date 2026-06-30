"""The packaged app serves the built SPA and the API on one origin (post-v1 launcher).

Skipped when the frontend hasn't been built (CI without ``npm run build``); the mount is a no-op
there, so the API still works — these tests just have nothing to assert.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.web import frontend_dist

pytestmark = pytest.mark.skipif(frontend_dist() is None,
                                reason="frontend not built (run `npm run build`)")


@pytest.fixture
def client():
    return TestClient(app)


def test_spa_shell_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="root"' in r.text  # the React mount point


def test_hashed_assets_served(client):
    bundle = re.search(r"/assets/([\w.\-]+\.js)", client.get("/").text)
    assert bundle, "no /assets/*.js referenced from index.html"
    r = client.get(f"/assets/{bundle.group(1)}")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_client_side_route_falls_back_to_shell(client):
    r = client.get("/some/deep/client/route")
    assert r.status_code == 200 and 'id="root"' in r.text


def test_api_and_infra_paths_are_not_shadowed(client):
    # reserved prefixes must NOT be served the SPA shell
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/health").status_code == 200  # explicit API route still wins
