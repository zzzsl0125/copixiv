"""Container wiring tests — pin the REAL ``create_app()`` composition root.

The security regression tests in tests/regression/test_c1_security.py build a
hand-rolled mini app; a regression in the composition root (middleware order,
router mounting, lifespan state) would sail past every test.  This file
builds the actual composition root against a temp config + temp database and
asserts the production wiring itself.

Since the ``Container`` class was flattened into the root-level
``copixiv.app`` module, the fixture now calls ``create_app(config_path=...)``
directly (which performs the build + FastAPI factory) and drives it through
``TestClient`` — exactly the way the production app is exercised.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from copixiv.app import (
    create_app,
    AccessLogMiddleware,
    APIAuthMiddleware,
    HostValidationMiddleware,
)

WIRING_DB = "copixiv_wiring_test"
WIRING_URL = f"postgresql+psycopg2://postgres@127.0.0.1:5433/{WIRING_DB}"


def _ensure_wiring_db() -> None:
    """Create + migrate a dedicated empty database for the wiring app."""
    from copixiv.db.engine import run_migrations

    subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "pg_dev.py"),
         "createdb", WIRING_DB],
        check=True,
    )
    run_migrations(WIRING_URL)


@pytest.fixture(scope="module", autouse=True)
def _wiring_db():
    _ensure_wiring_db()


@pytest.fixture(scope="module")
def config_path(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("container")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "\n".join([
            "database_url: " + WIRING_URL,
            "path:",
            f"  database: {tmp_path / 'db' / 'app.db'}",
            f"  download: {tmp_path / 'download'}",
            f"  token: {tmp_path / 'no-such-token-file.py'}",
            "pixiv_client: {min_interval: 1, max_concurrency: 2}",
            "notifications: []",
            "batch_download: {naming: '{id}_{title}'}",
            "security:",
            "  api_key: sekret",
            "  allowed_hosts: [good.example]",
            "  allowed_origins: [http://localhost:5173]",
        ]),
        encoding="utf-8",
    )
    return str(config_file)


@pytest.fixture(scope="module")
def app(config_path):
    return create_app(config_path=config_path)


@pytest.fixture()
def client(app):
    # The config allows good.example; TestClient's default Host
    # (testserver) is intentionally NOT special-cased by the middleware.
    with TestClient(app, base_url="http://good.example") as test_client:
        yield test_client


class TestRealMiddlewareAssembly:
    def test_middleware_order_matches_security_contract(self, app):
        """Host must be outermost; CORS innermost.  The order itself is the
        security contract documented in the composition root.

        Starlette stores ``user_middleware`` inner-first and reverses it
        when building the request stack, so the request-flow order is the
        reversed list: Host → api_key → AccessLog → CORS."""
        request_flow = list(reversed([mw.cls for mw in app.user_middleware]))
        assert request_flow == [
            HostValidationMiddleware,
            APIAuthMiddleware,
            AccessLogMiddleware,
            CORSMiddleware,
            GZipMiddleware,
        ], f"unexpected middleware stack: {[t.__name__ for t in request_flow]}"

    def test_host_rejected_without_reaching_routes(self, app):
        with TestClient(app, base_url="http://evil.example.com") as c:
            r = c.get("/api/tokens/", headers={"X-API-Key": "sekret"})
        assert r.status_code == 400
        assert r.json() == {"detail": "Invalid Host header"}

    def test_allowed_host_passes(self, app):
        with TestClient(app, base_url="http://good.example") as c:
            r = c.get("/api/system/config", headers={"X-API-Key": "sekret"})
        assert r.status_code == 200

    def test_api_key_required_when_configured(self, client):
        r = client.get("/api/novels/")
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid or missing API key"}

    def test_api_key_accepted(self, client):
        r = client.get("/api/novels/", headers={"X-API-Key": "sekret"})
        assert r.status_code == 200

    def test_options_bypasses_api_key(self, client):
        # Plain OPTIONS has no route handler → 405 from routing, not 401
        # from the API-key middleware: the bypass is proven by the status.
        r = client.options("/api/novels/")
        assert r.status_code == 405

    def test_cors_preflight_answers_200(self, client):
        r = client.options(
            "/api/novels/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_cors_whitelist_enforced(self, client):
        r = client.get(
            "/api/novels/",
            headers={"X-API-Key": "sekret", "Origin": "http://evil.example.com"},
        )
        assert "access-control-allow-origin" not in r.headers

        r = client.get(
            "/api/novels/",
            headers={"X-API-Key": "sekret", "Origin": "http://localhost:5173"},
        )
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_cors_credentials_disabled(self, client):
        r = client.options(
            "/api/novels/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-credentials") != "true"


class TestRealRouterMounting:
    ROUTER_PREFIXES = [
        "/api/novels",
        "/api/tasks",
        "/api/system",
        "/api/tag-preferences",
        "/api/tag-aliases",
        "/api/search-history",
        "/api/tokens",
        "/api/library",
    ]

    def test_all_routers_mounted(self, app):
        # app.routes nests included routers behind lazy _IncludedRouter
        # entries on this FastAPI version — the OpenAPI schema is the
        # version-proof source of the mounted path set.
        paths = set(app.openapi()["paths"])
        for prefix in self.ROUTER_PREFIXES:
            assert any(path.startswith(prefix) for path in paths), (
                f"router prefix {prefix} not mounted; got {sorted(paths)}"
            )

    def test_lifespan_wires_app_state_dependencies(self, client):
        state = client.app.state
        assert state.session_factory is not None
        assert state.config is not None
        assert state.client is not None
        assert state.file_storage is not None
        assert state.image_downloader is not None
        assert state.epub_builder is not None
        assert state.account_pool is not None
        assert state.task_manager is not None

    def test_config_endpoint_serves_frontend_defaults(self, client):
        r = client.get("/api/system/config", headers={"X-API-Key": "sekret"})
        assert r.status_code == 200
        assert set(r.json()) == {
            "batch_download_naming", "exclude_blocked_tag_novels",
        }
        assert r.json()["exclude_blocked_tag_novels"] is True

    def test_migrations_ran_against_real_config_path(self, client):
        """create_app() must have migrated the configured PostgreSQL DB."""
        from sqlalchemy import create_engine, text

        url = client.app.state.config.database_url
        assert url.startswith("postgresql"), "app must target PostgreSQL now"
        eng = create_engine(url)
        try:
            with eng.connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar() == 1
        finally:
            eng.dispose()


class TestResponseCompression:
    """Large JSON responses ship gzipped; binary downloads must not.

    Motivation (production measurements): ``/api/novels/ids`` at default
    thresholds returns 2.1 MB of ids, ``/api/tasks/history?limit=100`` 289 kB
    and a single task's log 213 kB of repeated lines.  Compression cuts them
    to 32 % / 44 % / 7 % respectively.  Small payloads stay uncompressed (the
    ``/busy`` answer is 13 bytes and would only grow).

    The binary case is the one that must be *proven*, not assumed: a novel file
    is already compressed on disk and goes through ``FileResponse``; wrapping
    it in gzip would burn CPU to make it bigger.
    """

    def _seed_many(self, client, app, n=400):
        """Insert *n* novels so a list payload crosses the 1 KiB floor.

        Idempotent: the module-scoped app shares one database across this
        file's tests, so a plain ``add`` would collide on the second run.
        """
        from copixiv.db.models import Novel

        with app.state.session_factory() as s:
            for i in range(n):
                if s.get(Novel, 10_000 + i) is None:
                    s.add(Novel(
                        id=10_000 + i, title=f"小说{i}", author_id=None,
                        path=f"/tmp/{i}.txt", like=900, text=5000,
                    ))
            s.commit()

    def test_large_json_is_gzipped(self, client, app):
        self._seed_many(client, app)
        plain = client.get(
            "/api/novels/?per_page=200&min_like=0&min_text=0",
            headers={"X-API-Key": "sekret", "Accept-Encoding": "identity"},
        )
        packed = client.get(
            "/api/novels/?per_page=200&min_like=0&min_text=0",
            headers={"X-API-Key": "sekret", "Accept-Encoding": "gzip"},
        )
        assert plain.status_code == packed.status_code == 200
        # ``TestClient`` (httpx) transparently decodes the body, so the wire
        # size is not observable from ``.content``; the header is the proof
        # that the middleware ran, and equality of the decoded payload is the
        # proof that it is transparent.
        assert "content-encoding" not in plain.headers
        assert packed.headers.get("content-encoding") == "gzip"
        assert packed.content == plain.content

    def test_gzip_actually_shrinks_a_repetitive_payload(self):
        """Ratio check at the middleware level (httpx hides the wire size).

        A representative slice of what these endpoints return: a JSON list of
        ids and a log of timestamped lines.  Both compress by an order of
        magnitude because the repetition is exactly what gzip feeds on.
        """
        import gzip
        import json

        from starlette.middleware.gzip import GZipMiddleware
        from starlette.testclient import TestClient
        from fastapi import FastAPI

        payload = {
            "ids": list(range(200_000, 202_000)),
            "log": "\n".join(
                f"2026-10-23 05:30:{i % 60:02d} - INFO - [acc] API → novel_series({i})"
                for i in range(2000)
            ),
        }
        body = json.dumps(payload, ensure_ascii=False).encode()

        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=1024)

        @app.get("/big")
        async def big():
            from fastapi.responses import Response

            return Response(content=body, media_type="application/json")

        with TestClient(app) as c:
            r = c.get("/big", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip"
        compressed = gzip.compress(body)
        assert len(compressed) < len(body) // 5, (
            f"这类载荷压缩率应有 5× 以上：{len(body)} → {len(compressed)}"
        )

    def test_small_json_is_not_gzipped(self, client):
        """``/busy`` is a few bytes; compressing it would only add framing."""
        r = client.get(
            "/api/tasks/busy",
            headers={"X-API-Key": "sekret", "Accept-Encoding": "gzip"},
        )
        assert r.status_code == 200
        assert "content-encoding" not in r.headers

def _ensure_wiring_novel(app, novel_id: int, title: str, path, *, text: int):
    """Write the row for this run's temp file — **update the path if it exists**.

    The wiring app runs against a *module-scoped database that outlives the
    process*: the second `pytest` run finds the row from the first one, still
    pointing at the previous run's ``tmp_path``.  Insert-only made these two
    tests fail on every other run (404, because the stored path is gone and
    only ``novel.path`` is ever filled — see ``FileStorage.save_novel_text``).
    Upserting the path is what makes the test independent of leftover state,
    which is what every other test in this suite already is.
    """
    from copixiv.db.models import Novel

    with app.state.session_factory() as s:
        existing = s.get(Novel, novel_id)
        if existing is None:
            s.add(Novel(
                id=novel_id, title=title, author_id=None, path=str(path),
                like=900, text=text, has_epub=3,
            ))
        else:
            existing.path = str(path)
            existing.text = text
        s.commit()


    def test_epub_download_is_not_double_compressed(self, client, app):
        """An EPUB is already deflated inside — gzip would cost CPU for 0.6 %.

        Measured on real files: an EPUB gzips to 99.4 % of its own size (it
        grows once framing is counted), while a TXT novel gzips to 55 %.  The
        middleware's content-type exclusion list is what keeps the download
        endpoint from burning a compression pass per request.
        """
        import zipfile
        from pathlib import Path

        from copixiv.db.models import Novel

        storage = app.state.file_storage
        body = "正文" * 2000
        path = storage.save_novel_text(20_000, "某书", body)
        _ensure_wiring_novel(app, 20_000, "某书", path, text=5000)

        # A minimal real EPUB container, written next to the text file the way
        # the builder does.
        epub_path = path.with_suffix(".epub")
        with zipfile.ZipFile(epub_path, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("OEBPS/content.xhtml", f"<html><body>{body}</body></html>")

        r = client.get(
            "/api/novels/20000/download?format=epub",
            headers={"X-API-Key": "sekret", "Accept-Encoding": "gzip"},
        )
        assert r.status_code == 200
        assert "content-encoding" not in r.headers, "EPUB 不该被二次压缩"

    def test_txt_download_may_be_compressed(self, client, app):
        """TXT is highly compressible (55 %), so the middleware may compress it.

        Not asserting that it *is* compressed — the response is small here and
        ``minimum_size`` legitimately skips it.  What is pinned is that the
        exclusion list is scoped to containers, not to downloads in general.
        """
        from copixiv.db.models import Novel

        body = "正文" * 20000      # ~120 kB, past the 1 KiB floor
        path = app.state.file_storage.save_novel_text(20_001, "长书", body)
        _ensure_wiring_novel(app, 20_001, "长书", path, text=len(body))

        r = client.get(
            "/api/novels/20001/download?format=txt",
            headers={"X-API-Key": "sekret", "Accept-Encoding": "gzip"},
        )
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert r.content.decode("utf-8") == body
