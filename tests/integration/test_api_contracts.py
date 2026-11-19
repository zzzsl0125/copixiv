"""Layer 3 integration tests: FastAPI middleware, route contracts, error mapping, and AccountPool reload."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.factories import make_test_app

from copixiv.core.draft import build_novel
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.middleware import HostValidationMiddleware


def _build_test_app(db_session_factory, tmp_path: Path, *, api_key: str = ""):
    return make_test_app(db_session_factory, tmp_path, api_key=api_key)


def test_security_middlewares_host_and_api_key(db_session_factory, tmp_path: Path):
    app = _build_test_app(db_session_factory, tmp_path, api_key="secret-xyz")
    with TestClient(app, base_url="http://localhost") as client:
        # Invalid Host rejected with 400 (even with valid API key or CORS preflight OPTIONS)
        bad_host = client.get(
            "/api/novels",
            headers={"Host": "evil.example", "X-API-Key": "secret-xyz"},
        )
        assert bad_host.status_code == 400

        bad_preflight = client.options(
            "/api/novels",
            headers={
                "Host": "evil.example",
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert bad_preflight.status_code == 400

        # Standard routes require X-API-Key header (?api_key query param is rejected)
        assert client.get("/api/novels").status_code == 401
        assert client.get("/api/novels?api_key=secret-xyz").status_code == 401
        assert client.get("/api/novels", headers={"X-API-Key": "wrong"}).status_code == 401

        # Valid X-API-Key allowed
        ok = client.get("/api/novels", headers={"X-API-Key": "secret-xyz"})
        assert ok.status_code == 200

        # Download routes reject missing/wrong key (401) and accept ?api_key query param (404 when absent)
        assert client.get("/api/novels/999999/download").status_code == 401
        assert client.get("/api/novels/export/999999/download").status_code == 401
        assert client.get("/api/novels/999999/download?api_key=wrong").status_code == 401
        assert client.get("/api/novels/999999/download?api_key=secret-xyz").status_code == 404
        assert client.get("/api/novels/export/999999/download?api_key=secret-xyz").status_code == 404

        # WebSocket with invalid Host rejected (1008 close)
        with (
            pytest.raises(WebSocketDisconnect) as ws_exc,
            client.websocket_connect("/ws", headers={"Host": "evil.example"}),
        ):
            pass
        assert ws_exc.value.code == 1008

    # Duplicate Host headers rejected at ASGI layer
    async def _run_dup_host() -> int:
        sent: list[dict] = []

        async def _dummy_app(_scope, _receive, _send):
            await _send({"type": "http.response.start", "status": 200, "headers": []})

        async def _recv():
            return {"type": "http.request"}

        async def _send(msg):
            sent.append(msg)

        mw = HostValidationMiddleware(_dummy_app)
        scope = {
            "type": "http",
            "headers": [(b"host", b"localhost"), (b"host", b"evil.example")],
        }
        await mw(scope, _recv, _send)
        return sent[0]["status"]

    assert asyncio.run(_run_dup_host()) == 400


def test_novels_query_validation_and_patch_policy(
    db_session_factory,
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=4242,
                    title="PixivBook",
                    author_id=7,
                    author_name="Auth7",
                    download_dir=str(tmp_path),
                )
            ],
        )

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        bad_query = client.get("/api/novels", params={"keyword": "id:notanint"})
        assert bad_query.status_code == 400

        non_dict_cursor = client.get("/api/novels", params={"cursor": "[]"})
        assert non_dict_cursor.status_code == 400

        bad_enum = client.get("/api/novels", params={"order_by": "invalid_col"})
        assert bad_enum.status_code == 422

        bad_patch = client.patch("/api/novels/4242", json={"author_id": 99})
        assert bad_patch.status_code == 400


def test_scheduled_tasks_clear_params_on_func_change_and_tolerate_legacy_cron(
    db_session_factory,
    tmp_path: Path,
):
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        legacy = models.ScheduledTask(
            name="legacy_bad_cron",
            task="check_epub",
            cron="invalid cron",
            params={"old": 1},
            is_enabled=False,
        )
        uow.session.add(legacy)
        uow.session.flush()
        legacy_id = legacy.id

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Listing tolerates legacy invalid cron row
        listed = client.get("/api/tasks/scheduled")
        assert listed.status_code == 200
        assert any(row["id"] == legacy_id for row in listed.json())

        # Create task with params, then switch task function without params -> clears old params
        created = client.post(
            "/api/tasks/scheduled",
            json={
                "name": "switch_test",
                "task": "novel_fetch",
                "cron": "0 3 * * *",
                "params": {"id": 12345},
                "is_enabled": False,
            },
        )
        assert created.status_code == 200
        tid = created.json()["id"]

        updated = client.put(
            f"/api/tasks/scheduled/{tid}",
            json={"task": "check_epub"},
        )
        assert updated.status_code == 200
        assert updated.json()["task"] == "check_epub"
        assert updated.json()["params"] is None


def test_tokens_lifecycle_masks_secrets_and_reloads_account_pool(
    db_session_factory,
    tmp_path: Path,
):
    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Create two tokens, marking the second as follow
        r1 = client.post(
            "/api/tokens",
            json={"name": "acc1", "token": "tok_abcdef1234", "premium": False, "valid": True},
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["token"] == "****1234"

        r2 = client.post(
            "/api/tokens",
            json={
                "name": "acc2",
                "token": "tok_secret9999",
                "premium": True,
                "valid": True,
                "is_follow": True,
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
        listing = client.get("/api/tokens").json()
        follow_map = {item["name"]: item["is_follow"] for item in listing}
        assert follow_map == {"acc1": True, "acc2": False}
        assert all("tok_" not in item["token"] for item in listing)

        pool_follow = {a.username: a.token_info.follow for a in app.state.account_pool.accounts}
        assert pool_follow == {"acc1": True, "acc2": False}

        # Delete acc2 -> immediately disappears from pool
        dele = client.delete(f"/api/tokens/{d2['id']}")
        assert dele.status_code == 204
        assert [a.username for a in app.state.account_pool.accounts] == ["acc1"]


def test_lifespan_defers_runtime_init_and_cleans_up_resources(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = _build_test_app(committed_session_factory, tmp_path)
    # create_app() only attaches config — DB/HTTP/Rust initialization runs in lifespan
    assert hasattr(app.state, "config")
    assert not hasattr(app.state, "http_client")
    assert not hasattr(app.state, "query_engine")

    detached: list[bool] = []
    from copixiv.db.novel_snapshot import PgSnapshotStore

    orig_detach = PgSnapshotStore.detach_runtime

    def _spy_detach(self):
        detached.append(True)
        orig_detach(self)

    monkeypatch.setattr(PgSnapshotStore, "detach_runtime", _spy_detach)

    with TestClient(app, base_url="http://localhost"):
        http_client = app.state.http_client
        assert http_client.is_closed is False
        assert app.state.query_engine.ready() is True

    assert http_client.is_closed is True
    assert detached == [True]

    # When lifespan owns the Engine (session_factory=None), exiting calls engine.dispose()
    import os

    from copixiv.app import create_app
    from copixiv.config import AppConfig, BackupConfig, PathConfig, SecurityConfig
    from copixiv.db.engine import create_database_engine

    test_db_url = os.environ.get(
        "COPIXIV_TEST_DATABASE_URL",
        "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv_test",
    )
    real_engine = create_database_engine(test_db_url)
    disposed: list[bool] = []
    orig_dispose = real_engine.dispose

    def _spy_dispose():
        disposed.append(True)
        orig_dispose()

    monkeypatch.setattr(real_engine, "dispose", _spy_dispose)
    monkeypatch.setattr("copixiv.lifespan.create_database_engine", lambda _url: real_engine)

    owned_app = create_app(
        config=AppConfig(
            database_url=test_db_url,
            path=PathConfig(download=str(tmp_path / "dl")),
            backup=BackupConfig(dir=str(tmp_path / "bk")),
            security=SecurityConfig(allowed_hosts=["localhost"]),
        ),
        log_dir=str(tmp_path / "logs"),
        skip_init_db=True,
        skip_auto_backup=True,
        mount_spa=False,
        run_background_tasks=False,
    )
    with TestClient(owned_app, base_url="http://localhost"):
        assert owned_app.state.engine is real_engine
    assert disposed == [True]


def test_integrity_error_maps_to_chinese_400(db_session_factory, tmp_path: Path):
    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Duplicate token name -> 400 with Chinese detail
        tok_payload = {"name": "dup_acc", "token": "tok_dup_1234", "premium": False, "valid": True}
        assert client.post("/api/tokens", json=tok_payload).status_code == 200
        dup_tok = client.post("/api/tokens", json=tok_payload)
        assert dup_tok.status_code == 400
        assert "同名账号已存在" in dup_tok.json()["detail"]

        # Duplicate tag preference -> 400 with Chinese detail
        pref_payload = {"tag": "dup_tag", "preference": "favourite", "sort_index": 0}
        assert client.post("/api/tag-preferences", json=pref_payload).status_code == 200
        dup_pref = client.post("/api/tag-preferences", json=pref_payload)
        assert dup_pref.status_code == 400
        assert "该标签偏好已存在" in dup_pref.json()["detail"]


def test_task_cancellation_and_uniform_204_deletes(db_session_factory, tmp_path: Path):
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        pending = models.TaskHistory(
            name="pending_test",
            task_func="novel_fetch",
            status="pending",
            start_time=datetime.now(UTC),
        )
        uow.session.add(pending)
        uow.session.add(
            models.SearchHistory(
                type="keyword",
                value="clear_me",
                timestamp=datetime.now(UTC),
            )
        )
        uow.session.flush()
        pending_id = pending.id

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        cancelled = client.post(f"/api/tasks/history/{pending_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json() == {"task_id": pending_id, "status": "cancelled"}
        assert client.post("/api/tasks/history/999999/cancel").status_code == 404

        pref = client.post(
            "/api/tag-preferences",
            json={"tag": "temp_pref", "preference": "blocked", "sort_index": 0},
        )
        assert pref.status_code == 200
        assert client.delete(f"/api/tag-preferences/{pref.json()['id']}").status_code == 204

        alias = client.post(
            "/api/tag-aliases",
            json={"source": "src_alias_tag", "target": "dst_alias_tag"},
        )
        assert alias.status_code == 200
        assert client.delete(f"/api/tag-aliases/{alias.json()['id']}").status_code == 204

        assert client.delete("/api/search-history").status_code == 204

        sched = client.post(
            "/api/tasks/scheduled",
            json={
                "name": "temp_sched",
                "task": "check_epub",
                "cron": "0 4 * * *",
                "is_enabled": False,
            },
        )
        assert sched.status_code == 200
        assert client.delete(f"/api/tasks/scheduled/{sched.json()['id']}").status_code == 204
