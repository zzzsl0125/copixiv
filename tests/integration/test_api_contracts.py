"""Layer 3 integration tests: FastAPI middleware, route contracts, error mapping, and AccountPool reload."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from copixiv.app import create_app
from copixiv.config import AppConfig, PathConfig, SecurityConfig
from copixiv.core.draft import build_novel
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.persist import persist_novels


def _build_test_app(db_session_factory, tmp_path: Path, *, api_key: str = ""):
    cfg = AppConfig(
        path=PathConfig(
            download=str(tmp_path / "download"),
            token=str(tmp_path / "nonexistent_tokens.py"),
        ),
        security=SecurityConfig(api_key=api_key, allowed_hosts=["localhost"]),
    )
    return create_app(
        config=cfg,
        log_dir=str(tmp_path / "logs"),
        session_factory=db_session_factory,
        skip_init_db=True,
        skip_auto_backup=True,
        mount_spa=False,
        run_background_tasks=False,
    )


def test_security_middlewares_host_and_api_key(db_session_factory, tmp_path: Path):
    app = _build_test_app(db_session_factory, tmp_path, api_key="secret-xyz")
    with TestClient(app, base_url="http://localhost") as client:
        # Invalid Host rejected with 400 (even with valid API key)
        bad_host = client.get(
            "/api/novels/",
            headers={"Host": "evil.example", "X-API-Key": "secret-xyz"},
        )
        assert bad_host.status_code == 400

        # Missing or wrong API key rejected with 401
        assert client.get("/api/novels/").status_code == 401
        assert client.get("/api/novels/", headers={"X-API-Key": "wrong"}).status_code == 401

        # Valid X-API-Key allowed
        ok = client.get("/api/novels/", headers={"X-API-Key": "secret-xyz"})
        assert ok.status_code == 200


def test_novels_query_validation_and_patch_policy(
    db_session_factory, uow: SqlUnitOfWork, tmp_path: Path,
):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=4242, title="PixivBook", author_id=7, author_name="Auth7",
                    download_dir=str(tmp_path),
                )
            ],
        )

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        bad_query = client.get("/api/novels/", params={"keyword": "id:notanint"})
        assert bad_query.status_code == 400

        bad_patch = client.patch("/api/novels/4242", json={"author_id": 99})
        assert bad_patch.status_code == 400


def test_tokens_lifecycle_masks_secrets_and_reloads_account_pool(
    db_session_factory, tmp_path: Path,
):
    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Create two tokens, marking the second as follow
        r1 = client.post(
            "/api/tokens/",
            json={"name": "acc1", "token": "tok_abcdef1234", "premium": False, "valid": True},
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["token"] == "****1234"

        r2 = client.post(
            "/api/tokens/",
            json={
                "name": "acc2", "token": "tok_secret9999",
                "premium": True, "valid": True, "is_follow": True,
            },
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["token"] == "****9999"
        assert d2["is_follow"] is True

        pool_accounts = {a.username: a for a in app.state.account_pool.accounts}
        assert set(pool_accounts) == {"acc1", "acc2"}
        assert pool_accounts["acc2"].token_info.follow is True

        # Switch follow to acc1 via PUT -> single-follow invariant & hot reload
        upd = client.put(f"/api/tokens/{d1['id']}", json={"is_follow": True})
        assert upd.status_code == 200
        listing = client.get("/api/tokens/").json()
        follow_map = {item["name"]: item["is_follow"] for item in listing}
        assert follow_map == {"acc1": True, "acc2": False}
        assert all("tok_" not in item["token"] for item in listing)

        pool_follow = {a.username: a.token_info.follow for a in app.state.account_pool.accounts}
        assert pool_follow == {"acc1": True, "acc2": False}

        # Delete acc2 -> immediately disappears from pool
        dele = client.delete(f"/api/tokens/{d2['id']}")
        assert dele.status_code == 200
        assert [a.username for a in app.state.account_pool.accounts] == ["acc1"]
