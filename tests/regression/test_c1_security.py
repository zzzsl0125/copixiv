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

from copixiv.app.config import AppConfig
from copixiv.domain.exceptions import DomainError
from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Token
from copixiv.web_api.endpoints import tokens
from copixiv.web_api.host_middleware import HostValidationMiddleware
from copixiv.web_api.api_key_middleware import APIAuthMiddleware


def _build_app(session_factory, config: AppConfig) -> FastAPI:
    """Build a minimal app wired like ``container.create_app()`` for tokens."""

    app = FastAPI(title="c1-security-test")

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail},
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
        r = client.get("/api/tokens/")
        assert r.status_code == 200


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

    def test_options_bypasses_api_key(self, keyed_client):
        r = keyed_client.options("/api/tokens/")
        assert r.status_code != 401
