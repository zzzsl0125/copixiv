"""Layer 3 integration tests: FastAPI middleware, route contracts, error mapping, and AccountPool reload."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
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
                "task": "source_fetch",
                "cron": "0 3 * * *",
                "params": {"refs": ["pixiv:12345"]},
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
        # Create two Pixiv credentials via /api/sources, marking the second as follow
        r1 = client.post(
            "/api/sources/pixiv/credentials",
            json={
                "name": "acc1",
                "kind": "refresh_token",
                "secret": {"refresh_token": "tok_abcdef1234"},
                "valid": True,
                "meta": {"premium": False, "is_follow": False},
            },
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["secret"]["refresh_token"] == "****1234"

        r2 = client.post(
            "/api/sources/pixiv/credentials",
            json={
                "name": "acc2",
                "kind": "refresh_token",
                "secret": {"refresh_token": "tok_secret9999"},
                "valid": True,
                "meta": {"premium": True, "is_follow": True},
            },
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["secret"]["refresh_token"] == "****9999"
        assert d2["meta"]["is_follow"] is True

        pool_accounts = {a.username: a for a in app.state.account_pool.accounts}
        assert set(pool_accounts) == {"acc1", "acc2"}
        assert pool_accounts["acc2"].token_info.follow is True

        # Switch follow to acc1 via PUT -> single-follow invariant & hot reload
        upd = client.put(f"/api/sources/pixiv/credentials/{d1['id']}", json={"meta": {"is_follow": True}})
        assert upd.status_code == 200
        listing = client.get("/api/sources/pixiv/credentials").json()
        follow_map = {item["name"]: item["meta"]["is_follow"] for item in listing}
        assert follow_map == {"acc1": True, "acc2": False}
        assert all("tok_" not in item["secret"]["refresh_token"] for item in listing)

        pool_follow = {a.username: a.token_info.follow for a in app.state.account_pool.accounts}
        assert pool_follow == {"acc1": True, "acc2": False}

        # Delete acc2 -> immediately disappears from pool
        dele = client.delete(f"/api/sources/pixiv/credentials/{d2['id']}")
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
        mount_spa=False,
        run_background_tasks=False,
    )
    with TestClient(owned_app, base_url="http://localhost"):
        assert owned_app.state.engine is real_engine
    assert disposed == [True]


def test_integrity_error_maps_to_chinese_400(db_session_factory, tmp_path: Path):
    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Duplicate credential name -> 400 with Chinese detail
        tok_payload = {
            "name": "dup_acc",
            "kind": "refresh_token",
            "secret": {"refresh_token": "tok_dup_1234"},
            "valid": True,
            "meta": {"premium": False},
        }
        assert client.post("/api/sources/pixiv/credentials", json=tok_payload).status_code == 200
        dup_tok = client.post("/api/sources/pixiv/credentials", json=tok_payload)
        assert dup_tok.status_code == 400
        assert "同名账号已存在" in dup_tok.json()["detail"]

        # Duplicate tag preference -> 400 with Chinese detail
        pref_payload = {"tag": "dup_tag", "preference": "favourite", "sort_index": 0}
        assert client.post("/api/tag-preferences", json=pref_payload).status_code == 200
        dup_pref = client.post("/api/tag-preferences", json=pref_payload)
        assert dup_pref.status_code == 400
        assert "该标签偏好已存在" in dup_pref.json()["detail"]

        # Duplicate tag alias source -> 400 with Chinese detail via tag_alias_source_key
        alias_payload = {"sources": ["dup_src"], "target": "dup_tgt"}
        assert client.post("/api/tag-aliases/batch", json=alias_payload).status_code == 200
        dup_alias = client.post("/api/tag-aliases/batch", json=alias_payload)
        assert dup_alias.status_code == 400
        assert "该标签别名已存在" in dup_alias.json()["detail"]


def test_task_cancellation_and_uniform_204_deletes(db_session_factory, tmp_path: Path):
    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        pending = models.TaskHistory(
            name="pending_test",
            task_func="source_fetch",
            status="pending",
            start_time=datetime.now(UTC),
        )
        with_log = models.TaskHistory(
            name="done_with_log",
            task_func="check_epub",
            arguments={"dry_run": False},
            status="success",
            start_time=datetime.now(UTC),
            result={"summary": "完成", "log": "line 1\nline 2"},
        )
        uow.session.add_all([pending, with_log])
        uow.session.add(
            models.SearchHistory(
                type="keyword",
                value="clear_me",
                timestamp=datetime.now(UTC),
            )
        )
        uow.session.flush()
        pending_id = pending.id
        with_log_id = with_log.id

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        methods_res = client.get("/api/tasks/methods")
        assert methods_res.status_code == 200
        methods_by_name = {m["name"]: m for m in methods_res.json()}
        assert "novel_fetch" not in methods_by_name
        assert "refetch_missing_source" not in methods_by_name
        assert "source_fetch" in methods_by_name
        assert isinstance(methods_by_name["source_fetch"]["arguments"], list)
        assert methods_by_name["source_fetch"]["source"] is None
        assert methods_by_name["novel_follow"]["source"] == "pixiv"
        assert methods_by_name["check_epub"]["arguments"] == []

        cancelled = client.post(f"/api/tasks/history/{pending_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json() == {"task_id": pending_id, "status": "cancelled"}
        assert client.post("/api/tasks/history/999999/cancel").status_code == 404

        hist = client.get("/api/tasks/history?limit=50&offset=0")
        assert hist.status_code == 200
        hist_items = {item["id"]: item for item in hist.json()["items"]}
        assert hist_items[with_log_id]["has_log"] is True
        assert hist_items[with_log_id]["arguments"] == {"dry_run": False}
        assert hist_items[with_log_id]["result"] == {"summary": "完成"}
        assert hist_items[pending_id]["has_log"] is False
        assert hist_items[pending_id]["result"] == {"summary": "任务已手动取消"}

        log_res = client.get(f"/api/tasks/history/{with_log_id}/log")
        assert log_res.status_code == 200
        assert log_res.json() == {"id": with_log_id, "log": "line 1\nline 2"}
        assert client.get(f"/api/tasks/history/{pending_id}/log").status_code == 404

        pref = client.post(
            "/api/tag-preferences",
            json={"tag": "temp_pref", "preference": "blocked", "sort_index": 0},
        )
        assert pref.status_code == 200
        assert client.delete(f"/api/tag-preferences/{pref.json()['id']}").status_code == 204

        alias = client.post(
            "/api/tag-aliases/batch",
            json={"sources": ["src_alias_tag"], "target": "dst_alias_tag"},
        )
        assert alias.status_code == 200
        assert len(alias.json()) == 1
        replaced = client.put(
            "/api/tag-aliases/groups",
            json={
                "old_target": "dst_alias_tag",
                "target": "dst_alias_tag_v2",
                "sources": ["src_alias_tag"],
            },
        )
        assert replaced.status_code == 200
        assert replaced.json()[0]["target"] == "dst_alias_tag_v2"
        assert (
            client.delete("/api/tag-aliases/groups", params={"target": "dst_alias_tag_v2"}).status_code == 204
        )

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


def test_token_validation_and_reorder_skips_pool_reload(
    db_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Whitespace-only token is rejected
        bad_create = client.post(
            "/api/sources/pixiv/credentials",
            json={
                "name": "blank",
                "kind": "refresh_token",
                "secret": {"refresh_token": "   "},
                "valid": True,
            },
        )
        assert bad_create.status_code == 400

        # Leading/trailing whitespace is stripped
        r1 = client.post(
            "/api/sources/pixiv/credentials",
            json={
                "name": "t1",
                "kind": "refresh_token",
                "secret": {"refresh_token": "  tok_strip_1234  "},
                "valid": True,
                "meta": {"premium": False},
            },
        )
        assert r1.status_code == 200
        t1_id = r1.json()["id"]
        assert r1.json()["secret"]["refresh_token"] == "****1234"

        # Whitespace-only secret on update is rejected (400); empty/masked secret preserves existing
        bad_upd = client.put(
            f"/api/sources/pixiv/credentials/{t1_id}",
            json={"secret": {"refresh_token": "   "}},
        )
        assert bad_upd.status_code == 400
        keep_upd = client.put(
            f"/api/sources/pixiv/credentials/{t1_id}",
            json={"secret": {"refresh_token": ""}},
        )
        assert keep_upd.status_code == 200
        assert keep_upd.json()["secret"]["refresh_token"] == "****1234"

        # Reorder must NOT trigger account_pool.reload_from_tokens
        reloads: list[object] = []
        monkeypatch.setattr(
            app.state.account_pool, "reload_from_tokens", lambda tokens: reloads.append(tokens)
        )
        reord = client.post("/api/sources/pixiv/credentials/reorder", json=[t1_id])
        assert reord.status_code == 200
        assert reloads == []


def test_failed_novels_list_join_and_removed_endpoints(
    db_session_factory,
    tmp_path: Path,
):
    from copixiv.features.failures.repo import FailedNovelRepository

    uow = SqlUnitOfWork(db_session_factory)
    with uow.begin():
        uow.session.add(models.Novel(id=501, title="Joined Novel Title", source_key="501"))
        repo = FailedNovelRepository(uow.session)
        repo.record(501, "download", "network timeout", title=None)
        repo.record(502, "download", "not found", title="Explicit Title")
        uow.session.commit()

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        res = client.get("/api/failed-novels")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 2
        # Actionable failure (501) first, "not found" (502) last; 501 inherits title from novel table
        assert [item["novel_id"] for item in data["items"]] == [501, 502]
        assert data["items"][0]["title"] == "Joined Novel Title"
        assert data["items"][1]["title"] == "Explicit Title"
        failed_id = data["items"][0]["id"]

        # 204 reset endpoints
        assert client.post(f"/api/failed-novels/{failed_id}/reset-count").status_code == 204
        assert client.post("/api/failed-novels/reset-count").status_code == 204

        # Removed dead endpoints
        assert client.get("/api/failed-novels/count").status_code == 404
        assert client.delete("/api/failed-novels/501").status_code == 404
        assert client.delete("/api/failed-novels").status_code == 405


def test_batch_scope_ids_only_and_export_contracts(
    db_session_factory,
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    from tests.factories import make_local_novel

    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=501,
                    title="PixivBook",
                    author_id=7,
                    author_name="Auth7",
                    download_dir=str(tmp_path),
                )
            ],
        )
        uow.session.add(
            make_local_novel(
                -501,
                title="LocalBook",
                author_id=7,
                author_name="Auth7",
                path=str(tmp_path / "download" / "local" / "LocalBook_-501.txt"),
            )
        )

    app = _build_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Empty scope rejected with 400 across batch, batch-metadata, and batch-export
        assert (
            client.post(
                "/api/novels/batch",
                json={"operation": "add_tags", "scope": {"novel_ids": []}, "tags": ["t"]},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/novels/batch-metadata",
                json={"scope": {"novel_ids": []}, "apply": False},
            ).status_code
            == 400
        )
        assert client.post("/api/novels/batch-export", json={"novel_ids": []}).status_code == 400

        # Duplicate IDs are deduplicated and resolved cleanly
        op_res = client.post(
            "/api/novels/batch",
            json={"operation": "add_tags", "scope": {"novel_ids": [501, 501]}, "tags": ["batch_ok"]},
        )
        assert op_res.status_code == 200
        assert op_res.json() == {"matched": 1, "affected": 1}

        meta_res = client.post(
            "/api/novels/batch-metadata",
            json={"scope": {"novel_ids": [-501, 501, 501]}, "apply": False},
        )
        assert meta_res.status_code == 200
        assert meta_res.json() == {"matched": 2, "applicable": 1, "skipped": 1, "affected": 0}

        # Invalid naming template rejected synchronously on both preview and export enqueue
        assert (
            client.post(
                "/api/novels/batch-export/preview",
                json={"novel_ids": [501], "naming_template": "{title}"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/novels/batch-export",
                json={"novel_ids": [501], "naming_template": "{title}"},
            ).status_code
            == 400
        )

        # Default naming template fallback from config works when naming_template is omitted
        prev_res = client.post("/api/novels/batch-export/preview", json={"novel_ids": [501]})
        assert prev_res.status_code == 200
        assert prev_res.json() == {"path": "Auth7/PixivBook_501.txt"}

        exp_res = client.post("/api/novels/batch-export", json={"novel_ids": [501, -501, 501]})
        assert exp_res.status_code == 200
        assert exp_res.json()["matched"] == 2
        assert isinstance(exp_res.json()["task_id"], int)
