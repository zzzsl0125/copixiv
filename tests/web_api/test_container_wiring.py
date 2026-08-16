"""Container wiring tests — pin the REAL ``container.create_app()`` assembly.

The security regression tests in tests/regression/test_c1_security.py build a
hand-rolled mini app; a regression in the composition root (middleware order,
router mounting, lifespan state) would sail past every test.  This file
builds the actual ``Container`` against a temp config + temp database and
asserts the production wiring itself.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from copixiv.app.container import Container
from copixiv.web_api.api_key_middleware import APIAuthMiddleware
from copixiv.web_api.host_middleware import HostValidationMiddleware
from copixiv.web_api.middleware import AccessLogMiddleware


@pytest.fixture(scope="module")
def container(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("container")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join([
            "path:",
            f"  database: {tmp_path / 'db' / 'app.db'}",
            f"  download: {tmp_path / 'download'}",
            f"  token: {tmp_path / 'no-such-token-file.py'}",
            "pixiv_client: {min_interval: 1, max_concurrency: 2}",
            "telegram: {token: '', chat_id: ''}",
            "frontend: {default_min_like: 500, default_min_text: 3000}",
            "batch_download: {naming: '{id}_{title}'}",
            "pixiv_accounts: {follow: ''}",
            "security:",
            "  api_key: sekret",
            "  allowed_hosts: [good.example]",
            "  allowed_origins: [http://localhost:5173]",
        ]),
        encoding="utf-8",
    )
    c = Container(config_path=str(config_path))
    c.build()
    yield c
    c.shutdown()


@pytest.fixture(scope="module")
def app(container):
    return container.create_app()


@pytest.fixture()
def client(app):
    # The container config allows good.example; TestClient's default Host
    # (testserver) is intentionally NOT special-cased by the middleware.
    with TestClient(app, base_url="http://good.example") as test_client:
        yield test_client


class TestRealMiddlewareAssembly:
    def test_middleware_order_matches_security_contract(self, app):
        """Host must be outermost; CORS innermost.  The order itself is the
        security contract documented in container.create_app().

        Starlette stores ``user_middleware`` inner-first and reverses it
        when building the request stack, so the request-flow order is the
        reversed list: Host → api_key → AccessLog → CORS."""
        request_flow = list(reversed([mw.cls for mw in app.user_middleware]))
        assert request_flow == [
            HostValidationMiddleware,
            APIAuthMiddleware,
            AccessLogMiddleware,
            CORSMiddleware,
        ], f"unexpected middleware stack: {[t.__name__ for t in request_flow]}"

    def test_host_rejected_without_reaching_routes(self, container):
        app = container.create_app()
        with TestClient(app, base_url="http://evil.example.com") as c:
            r = c.get("/api/tokens/", headers={"X-API-Key": "sekret"})
        assert r.status_code == 400
        assert r.json() == {"detail": "Invalid Host header"}

    def test_allowed_host_passes(self, container):
        app = container.create_app()
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
            "default_min_like", "default_min_text", "batch_download_naming",
            "exclude_blocked_tag_novels",
        }
        assert r.json()["exclude_blocked_tag_novels"] is True

    def test_migrations_ran_against_real_config_path(self, container):
        """The container's build must have created the configured database."""
        from pathlib import Path

        db_path = Path(container._engine.url.database)
        assert db_path.exists(), "Alembic migrations did not create the DB file"
