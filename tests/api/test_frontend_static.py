"""``mount_frontend`` — the SPA serving contract.

The backend serves ``frontend/dist`` itself (deploy/copixiv.service runs no
Node process), so the routes that used to belong to ``vite preview`` are now
production behaviour worth pinning:

* ``/assets/<hashed>`` is immutable-cached and path-contained;
* every other path falls back to ``index.html`` (``createWebHistory``);
* ``/api/...`` is **not** swallowed by the fallback — an unknown API path must
  keep returning FastAPI's JSON 404, not an HTML page;
* a missing bundle degrades to API-only instead of raising.

These tests build a throwaway ``dist`` and a bare ``FastAPI()`` — no database,
no config, no lifespan.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copixiv.app import mount_frontend

INDEX = "<!doctype html><html><body><div id='app'></div></body></html>"


def _dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    (tmp_path / "assets" / "app-abc123.js").write_text(
        "console.log('bundle')", encoding="utf-8"
    )
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


def _client(dist: Path) -> TestClient:
    app = FastAPI()
    assert mount_frontend(app, dist=dist) is True
    return TestClient(app)


def test_index_and_spa_fallback(tmp_path):
    client = _client(_dist(tmp_path))

    root = client.get("/")
    assert root.status_code == 200
    assert root.text == INDEX
    assert root.headers["cache-control"] == "no-cache"

    # A client-side route must survive a browser refresh.
    deep = client.get("/novels/12")
    assert deep.status_code == 200
    assert deep.text == INDEX


def test_assets_are_immutable_and_typed(tmp_path):
    client = _client(_dist(tmp_path))

    r = client.get("/assets/app-abc123.js")
    assert r.status_code == 200
    assert r.text == "console.log('bundle')"
    assert "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_root_files_are_served_verbatim(tmp_path):
    client = _client(_dist(tmp_path))

    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"


def test_unknown_api_path_keeps_json_404(tmp_path):
    client = _client(_dist(tmp_path))

    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
    assert r.headers["content-type"].startswith("application/json")


def test_asset_path_traversal_is_refused(tmp_path):
    dist = _dist(tmp_path)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    client = _client(dist)

    # Encoded so the client does not normalise it away before routing.
    r = client.get("/assets/%2e%2e/secret.txt")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_missing_bundle_is_api_only(tmp_path):
    app = FastAPI()
    assert mount_frontend(app, dist=tmp_path / "nope") is False

    # Nothing was mounted: the root is a plain 404, not an index fallback.
    assert TestClient(app).get("/").status_code == 404
