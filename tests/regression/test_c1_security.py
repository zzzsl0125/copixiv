"""C1 复现与回归：CORS 白名单 / Host 校验 / API key / refresh_token 脱敏。

基线问题：
- CORS ``allow_origins=["*"]`` 允许任意站点跨域读取
- 无 Host 头校验 → DNS rebinding 可绕过同源限制访问局域网服务
- ``GET /api/tokens`` 明文返回全部 Pixiv ``refresh_token``

本文件按「先失败后通过」编写：先锁定修复后的行为，再实现修复。
"""

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError
from copixiv.db.engine import create_session_factory
from copixiv.db.models import Base, Token
from copixiv.features.accounts import api as tokens
from copixiv.app import (
    HostValidationMiddleware, _domain_error_http_status, _normalize_host,
)
from copixiv.app import APIAuthMiddleware


def _build_app(session_factory, config: AppConfig) -> FastAPI:
    """Build a minimal app wired like ``container.create_app()`` for tokens."""

    app = FastAPI(title="c1-security-test")

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = config

    # Starlette 的 add_middleware 用 insert(0, ...)：先 add 的在最外层。
    # 顺序：Host（最外层）→ api_key → CORS（最内层）。
    app.add_middleware(
        HostValidationMiddleware,
        allowed_hosts=config.security.allowed_hosts,
    )
    app.add_middleware(APIAuthMiddleware, api_key=config.security.api_key)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.security.allowed_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(tokens.router, prefix="/api/tokens", tags=["tokens"])
    return app


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    # Only the tokens table is exercised here; skip the other (FTS-heavy)
    # tables so each test keeps a fast file-backed SQLite fixture.
    Base.metadata.create_all(bind=engine, tables=[Token.__table__])
    return create_session_factory(engine)


@pytest.fixture
def client(session_factory):
    config = AppConfig()
    config.security.allowed_hosts = ["testserver"]
    app = _build_app(session_factory, config)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def keyed_client(session_factory):
    config = AppConfig()
    config.security.allowed_hosts = ["testserver"]
    config.security.api_key = "sekret"
    app = _build_app(session_factory, config)
    with TestClient(app) as c:
        yield c


def _seed_token(session_factory, name="acct1", token="abcdef1234567890"):
    with session_factory() as s:
        s.add(Token(name=name, token=token, premium=False, valid=True))
        s.commit()


class TestTokenMasking:
    def test_get_tokens_masks_token(self, client, session_factory):
        _seed_token(session_factory)
        r = client.get("/api/tokens/")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and len(body) == 1
        assert body[0]["token"] == "****7890"
        assert "abcdef1234567890" not in r.text

    def test_create_token_masks_token(self, client):
        r = client.post("/api/tokens/", json={
            "name": "acct2", "token": "secret-token-value",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["token"] == "****alue"
        assert "secret-token-value" not in r.text

    def test_update_token_masks_token(self, client, session_factory):
        _seed_token(session_factory)
        token_id = client.get("/api/tokens/").json()[0]["id"]
        r = client.put(f"/api/tokens/{token_id}", json={"token": "updated-secret-999"})
        assert r.status_code == 200
        body = r.json()
        assert body["token"] == "****-999"
        assert "updated-secret-999" not in r.text

    def test_short_token_masks_to_just_stars(self, client, session_factory):
        """Tokens with ≤4 chars must not leak any suffix."""
        _seed_token(session_factory, name="short", token="abcd")
        r = client.get("/api/tokens/")
        body = [t for t in r.json() if t["name"] == "short"][0]
        assert body["token"] == "****"
        assert "abcd" not in r.text

    def test_update_without_token_keeps_secret(self, client, session_factory):
        """Editing name/premium/valid must not overwrite the stored token."""
        _seed_token(session_factory)
        token_id = client.get("/api/tokens/").json()[0]["id"]
        r = client.put(f"/api/tokens/{token_id}", json={"name": "renamed"})
        assert r.status_code == 200
        assert r.json()["token"] == "****7890"
        with session_factory() as s:
            stored = s.get(Token, token_id)
            assert stored.token == "abcdef1234567890", (
                "token was overwritten by a name-only update"
            )

    def test_delete_and_reorder(self, client, session_factory):
        _seed_token(session_factory, name="a1")
        _seed_token(session_factory, name="a2")
        ids = [t["id"] for t in client.get("/api/tokens/").json()]

        r = client.post("/api/tokens/reorder/", json=list(reversed(ids)))
        assert r.status_code == 200

        r = client.delete(f"/api/tokens/{ids[0]}")
        assert r.status_code == 200
        remaining = client.get("/api/tokens/").json()
        assert [t["id"] for t in remaining] == [ids[1]]

    def test_delete_missing_token_is_404(self, client):
        r = client.delete("/api/tokens/999")
        assert r.status_code == 404


class TestTokenFollowDesignation:
    def test_designating_follow_clears_others(self, client, session_factory):
        """At most one account is designated「追更账号」at a time."""
        _seed_token(session_factory, name="a1")
        _seed_token(session_factory, name="a2")
        tokens = client.get("/api/tokens/").json()
        id_a1 = next(t["id"] for t in tokens if t["name"] == "a1")
        id_a2 = next(t["id"] for t in tokens if t["name"] == "a2")
        assert all(not t["is_follow"] for t in tokens)

        r = client.put(f"/api/tokens/{id_a1}", json={"is_follow": True})
        assert r.status_code == 200
        assert r.json()["is_follow"] is True
        after = {t["name"]: t["is_follow"] for t in client.get("/api/tokens/").json()}
        assert after == {"a1": True, "a2": False}

        # designating a2 flips a1 off (singleton)
        r = client.put(f"/api/tokens/{id_a2}", json={"is_follow": True})
        assert r.status_code == 200
        after = {t["name"]: t["is_follow"] for t in client.get("/api/tokens/").json()}
        assert after == {"a1": False, "a2": True}

        # un-designating clears the flag entirely
        r = client.put(f"/api/tokens/{id_a2}", json={"is_follow": False})
        assert r.status_code == 200
        after = {t["name"]: t["is_follow"] for t in client.get("/api/tokens/").json()}
        assert after == {"a1": False, "a2": False}

    def test_follow_flag_persists_after_name_update(self, client, session_factory):
        _seed_token(session_factory, name="a1")
        token_id = client.get("/api/tokens/").json()[0]["id"]
        client.put(f"/api/tokens/{token_id}", json={"is_follow": True})
        r = client.put(f"/api/tokens/{token_id}", json={"name": "renamed"})
        assert r.status_code == 200
        assert r.json()["is_follow"] is True


class TestHostValidation:
    def test_evil_host_rejected(self, session_factory):
        config = AppConfig()
        config.security.allowed_hosts = ["testserver"]
        app = _build_app(session_factory, config)
        with TestClient(app, base_url="http://evil.example.com") as c:
            r = c.get("/api/tokens/")
            assert r.status_code == 400
            assert r.json() == {"detail": "Invalid Host header"}

    def test_testserver_host_allowed(self, client):
        # testserver passes because the fixture config explicitly allows it
        # — not because the middleware special-cases test hosts.
        r = client.get("/api/tokens/")
        assert r.status_code == 200

    def test_testserver_not_special_cased_without_config(self, session_factory):
        """TestClient's default Host must NOT be magically allow-listed:
        without an explicit entry in allowed_hosts it is rejected."""
        config = AppConfig()
        config.security.allowed_hosts = []  # no testserver
        app = _build_app(session_factory, config)
        with TestClient(app) as c:  # default Host header: testserver
            r = c.get("/api/tokens/")
        assert r.status_code == 400
        assert r.json() == {"detail": "Invalid Host header"}


class TestHostNormalizationBranches:
    """Port stripping / IPv6 / IP literals / allow-list casing."""

    def _mw(self, allowed_hosts=None) -> HostValidationMiddleware:
        return HostValidationMiddleware(None, allowed_hosts or [])

    def test_port_is_stripped(self):
        assert _normalize_host("good.example:8000") == "good.example"
        assert _normalize_host("127.0.0.1:8000") == "127.0.0.1"

    def test_ipv6_brackets_stripped_but_bare_ipv6_kept(self):
        assert _normalize_host("[::1]:8000") == "::1"
        assert _normalize_host("::1") == "::1"

    def test_ip_literals_always_allowed(self):
        mw = self._mw()
        assert mw._is_allowed("192.168.1.10") is True
        assert mw._is_allowed("::1") is True

    def test_localhost_always_allowed(self):
        assert self._mw()._is_allowed("localhost") is True

    def test_allowlist_is_case_insensitive(self):
        mw = self._mw(["Good.Example"])
        assert mw._is_allowed("good.example") is True
        assert mw._is_allowed("GOOD.EXAMPLE") is True
        assert mw._is_allowed("evil.example") is False


class TestCors:
    def test_evil_origin_not_allowed(self, client):
        r = client.get("/api/tokens/", headers={"Origin": "http://evil.example.com"})
        assert r.status_code == 200
        assert (
            "access-control-allow-origin" not in r.headers
            or "http://evil.example.com" not in r.headers["access-control-allow-origin"]
        )

    def test_allowlisted_origin_echoed(self, client):
        r = client.get("/api/tokens/", headers={"Origin": "http://localhost:5173"})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


class TestApiKey:
    def test_missing_key_401(self, keyed_client):
        r = keyed_client.get("/api/tokens/")
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid or missing API key"}

    def test_wrong_key_401(self, keyed_client):
        r = keyed_client.get("/api/tokens/", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_correct_key_200(self, keyed_client):
        r = keyed_client.get("/api/tokens/", headers={"X-API-Key": "sekret"})
        assert r.status_code == 200

    def test_key_via_query_param_accepts(self, keyed_client):
        # Navigation-based downloads can't set an X-API-Key header; the
        # frontend appends ?api_key= instead. The key is already bundled in
        # the client, so this is not an additional leak.
        r = keyed_client.get("/api/tokens/?api_key=sekret")
        assert r.status_code == 200

    def test_wrong_key_query_param_401(self, keyed_client):
        r = keyed_client.get("/api/tokens/?api_key=wrong")
        assert r.status_code == 401

    def test_options_bypasses_api_key(self, keyed_client):
        # Plain OPTIONS has no route handler → 405 from routing, not 401
        # from the API-key middleware: the bypass is proven by the status.
        r = keyed_client.options("/api/tokens/")
        assert r.status_code == 405

    def test_cors_preflight_answered_without_key(self, keyed_client):
        r = keyed_client.options(
            "/api/tokens/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200

    def test_empty_api_key_disables_check(self, session_factory):
        config = AppConfig()
        config.security.allowed_hosts = ["testserver"]
        config.security.api_key = ""  # default: auth disabled
        app = _build_app(session_factory, config)
        with TestClient(app) as c:
            assert c.get("/api/tokens/").status_code == 200
