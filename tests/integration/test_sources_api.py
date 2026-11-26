"""Integration tests for ``/api/sources``, source toggling/hot-reload, and multi-source services."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import FakePixivClient
from tests.factories import make_local_novel, make_test_app

from copixiv.core.draft import build_novel
from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_ESJ, SOURCE_LOCAL, SOURCE_PIXIV, EpubStatus
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.service import delete_author
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.features.novels.use_cases.discovery import fetch_special_follow_novels, retry_failed_novels
from copixiv.features.novels.use_cases.refetch_missing import refetch_missing_novels
from copixiv.features.sources.entity_resolver import EntityResolver
from copixiv.features.sources.service import SourceService
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.features.tags.resolver import TagAliasResolver
from copixiv.pixiv.account import PIXIV_AUTH_URL
from copixiv.sources import LocalSourcePlugin, PixivSourcePlugin, SourceHttpManager, SourceRegistry
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.batch import BatchExportArgs, batch_export
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.maintenance.series import FixSeriesIndexArgs, fix_series_index
from copixiv.tasks.novels import author_special_follow


class _RecordingPixivClient(FakePixivClient):
    """``FakePixivClient`` extended with ``user_follow_delete``, ``reconfigure``, and rule tracking."""

    def __init__(self) -> None:
        super().__init__()
        self.follow_rules: list[dict[str, bool]] = []
        self.max_concurrency: int = 5

    def reconfigure(self, *, max_concurrency: int | None = None) -> None:
        if max_concurrency is not None:
            self.max_concurrency = max(1, int(max_concurrency))

    @asynccontextmanager
    async def account_rule(self, **kwargs):
        self.follow_rules.append({k: bool(v) for k, v in kwargs.items()})
        yield self

    async def user_follow_delete(self, user_id: int) -> dict[str, Any]:
        self.calls.append(("user_follow_delete", user_id))
        self._maybe_raise("user_follow_delete", user_id)
        return {}


def _create_pixiv_credential(
    client: TestClient,
    *,
    name: str,
    refresh_token: str,
    valid: bool = True,
    premium: bool = False,
    is_follow: bool = False,
) -> dict[str, Any]:
    resp = client.post(
        "/api/sources/pixiv/credentials",
        json={
            "name": name,
            "kind": "refresh_token",
            "secret": {"refresh_token": refresh_token},
            "valid": valid,
            "meta": {"premium": premium, "is_follow": is_follow},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _make_novel_info(
    novel_id: int,
    *,
    title: str,
    author_id: int,
    author_name: str,
    tags: list[str] | None = None,
    caption: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=novel_id,
        title=title,
        caption=caption,
        tags=[SimpleNamespace(name=t) for t in (tags or [])],
        user=SimpleNamespace(id=author_id, name=author_name),
        series=None,
        total_bookmarks=10,
        total_view=100,
        text_length=20,
        create_date="2026-01-10T00:00:00+09:00",
    )


def _make_webview_novel(
    novel_id: int,
    *,
    title: str,
    author_id: int,
    text: str = "测试小说正文内容",
    tags: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=novel_id,
        title=title,
        user_id=author_id,
        rating={"bookmark": 10, "view": 100},
        caption="中文简介",
        series_id=None,
        series_title=None,
        series_navigation=None,
        cdate="2026-01-10T00:00:00+09:00",
        tags=list(tags or ["中文"]),
        text=text,
        images=[],
        illusts=[],
        cover_url=None,
    )


# ---------------------------------------------------------------------------
# 1. HTTP /api/sources & multi-source runtime behavior via TestClient
# ---------------------------------------------------------------------------


def test_list_sources_returns_seeded_manifests_and_credential_counts(
    committed_session_factory,
    tmp_path: Path,
):
    """``GET /api/sources`` returns seeded pixiv, local & esj manifests, defaults, and credential counts."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        res = client.get("/api/sources")
        assert res.status_code == 200
        by_source = {item["source"]: item for item in res.json()}
        assert set(by_source) == {SOURCE_PIXIV, SOURCE_LOCAL, SOURCE_ESJ}

        esj = by_source[SOURCE_ESJ]
        assert esj["enabled"] is False
        assert esj["manifest"]["epub_policy"] == "best_effort_external"
        assert esj["manifest"]["badge_label"] == "ESJ"

        pixiv = by_source[SOURCE_PIXIV]
        assert pixiv["enabled"] is True
        assert pixiv["credential_count"] == 0
        assert pixiv["valid_credential_count"] == 0
        pixiv_manifest = pixiv["manifest"]
        assert pixiv_manifest["epub_policy"] == "strict_placeholders"
        assert pixiv_manifest["editable_fields"] == ["series_index", "tags", "create_time"]
        assert pixiv_manifest["shelf"]["section_id"] == "source_pixiv"
        assert {f["key"] for f in pixiv_manifest["settings_schema"]} == {
            "proxy_mode",
            "proxy_url",
            "min_interval",
            "cooling_duration",
            "max_concurrency",
        }
        assert len(pixiv_manifest["credential_kinds"]) == 1
        assert pixiv_manifest["credential_kinds"][0]["kind"] == "refresh_token"
        assert pixiv_manifest["credential_kinds"][0]["unique_meta_flags"] == ["is_follow"]

        local = by_source[SOURCE_LOCAL]
        assert local["enabled"] is True
        assert local["credential_count"] == 0
        assert local["valid_credential_count"] == 0
        local_manifest = local["manifest"]
        assert local_manifest["epub_policy"] == "container_only"
        assert local_manifest["editable_fields"] == [
            "title",
            "author_id",
            "series_id",
            "series_index",
            "tags",
            "create_time",
        ]
        assert local_manifest["credential_kinds"] == []
        assert local_manifest["shelf"]["section_id"] == "source_local"
        assert {f["key"] for f in local_manifest["settings_schema"]} == {
            "parse_filename_metadata",
            "default_unknown_author",
        }

        # Add 1 valid + 1 invalid credential -> counts update accordingly
        _create_pixiv_credential(client, name="valid_acc", refresh_token="tok_valid_1111", valid=True)
        _create_pixiv_credential(client, name="invalid_acc", refresh_token="tok_invalid_2222", valid=False)

        updated = {item["source"]: item for item in client.get("/api/sources").json()}
        assert updated[SOURCE_PIXIV]["credential_count"] == 2
        assert updated[SOURCE_PIXIV]["valid_credential_count"] == 1

        # Unknown source -> 404
        assert client.put("/api/sources/nonexistent/config", json={"enabled": False}).status_code == 404
        assert client.get("/api/sources/nonexistent/credentials").status_code == 404


def test_local_config_update_persists_and_controls_upload_metadata(
    committed_session_factory,
    tmp_path: Path,
):
    """Updating ``local`` settings persists to DB and controls filename/author parsing on upload."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Default settings: parse_filename_metadata=True
        default_up = client.post(
            "/api/library/uploads",
            files=[("files", ("【作者X】默认解析.txt", "默认解析正文内容".encode(), "text/plain"))],
        )
        assert default_up.status_code == 200
        default_item = default_up.json()["results"][0]
        assert default_item["status"] == "created"
        assert default_item["title"] == "默认解析"
        assert default_item["author_name"] == "作者X"

        # Toggle settings via PUT /api/sources/local/config
        cfg_res = client.put(
            "/api/sources/local/config",
            json={
                "config": {
                    "parse_filename_metadata": False,
                    "default_unknown_author": "佚名",
                }
            },
        )
        assert cfg_res.status_code == 200
        cfg_body = cfg_res.json()
        assert cfg_body["enabled"] is True
        assert cfg_body["config"]["parse_filename_metadata"] is False
        assert cfg_body["config"]["default_unknown_author"] == "佚名"

        # Verify persisted in GET /api/sources and DB source_config row
        sources_map = {item["source"]: item for item in client.get("/api/sources").json()}
        assert sources_map[SOURCE_LOCAL]["config"]["parse_filename_metadata"] is False
        assert sources_map[SOURCE_LOCAL]["config"]["default_unknown_author"] == "佚名"
        with committed_session_factory() as session:
            row = session.get(models.SourceConfig, SOURCE_LOCAL)
            assert row is not None
            assert row.config["parse_filename_metadata"] is False
            assert row.config["default_unknown_author"] == "佚名"

        # Upload with updated settings -> preserves bracketed filename stem & uses "佚名"
        raw_up = client.post(
            "/api/library/uploads",
            files=[("files", ("【作者A】书名.txt", "关闭文件名解析后的正文内容".encode(), "text/plain"))],
        )
        assert raw_up.status_code == 200
        raw_item = raw_up.json()["results"][0]
        assert raw_item["status"] == "created"
        assert raw_item["title"] == "【作者A】书名"
        assert raw_item["author_name"] == "佚名"

        by_id = client.post("/api/novels/by-ids", json={"novel_ids": [raw_item["novel_id"]]}).json()
        assert len(by_id["novels"]) == 1
        assert by_id["novels"][0]["title"] == "【作者A】书名"
        assert by_id["novels"][0]["author_name"] == "佚名"


def test_disable_local_source_blocks_upload_but_allows_browse_edit_download_and_export(
    committed_session_factory,
    tmp_path: Path,
):
    """Disabling ``local`` rejects new uploads (400) while offline library operations stay functional."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        up = client.post(
            "/api/library/uploads",
            files=[("files", ("【原著作者】离线本地书.txt", "本地书正文内容".encode(), "text/plain"))],
        )
        assert up.status_code == 200
        local_id = up.json()["results"][0]["novel_id"]
        assert local_id < 0

        # Disable local source
        dis = client.put("/api/sources/local/config", json={"enabled": False})
        assert dis.status_code == 200
        assert dis.json()["enabled"] is False

        # New uploads rejected with 400
        blocked_up = client.post(
            "/api/library/uploads",
            files=[("files", ("new.txt", "新增内容".encode(), "text/plain"))],
        )
        assert blocked_up.status_code == 400
        assert "已禁用" in blocked_up.json()["detail"]

        # Browsing still works
        browse = client.get("/api/novels", params={"keyword": "source:local;"})
        assert browse.status_code == 200
        assert any(n["id"] == local_id for n in browse.json()["novels"])

        # Editing local metadata still works
        patched = client.patch(
            f"/api/novels/{local_id}",
            json={"title": "改名后的本地书", "tags": ["本地标签"], "series_index": 3},
        )
        assert patched.status_code == 200
        assert patched.json()["title"] == "改名后的本地书"
        assert patched.json()["tags"] == ["本地标签"]
        assert patched.json()["series_index"] == 3

        # Single download still works
        dl = client.get(f"/api/novels/{local_id}/download")
        assert dl.status_code == 200
        assert "本地书正文内容" in dl.text

        # Batch export preview + enqueue + execution + download still work
        preview = client.post("/api/novels/batch-export/preview", json={"novel_ids": [local_id]})
        assert preview.status_code == 200
        assert preview.json()["path"] is not None

        export_req = client.post(
            "/api/novels/batch-export",
            json={"novel_ids": [local_id], "zip_name": "local_disabled_export"},
        )
        assert export_req.status_code == 200
        task_id = export_req.json()["task_id"]
        assert export_req.json()["matched"] == 1

        ctx = TaskContext(
            uow=SqlUnitOfWork(committed_session_factory),
            file_storage=app.state.file_storage,
            task_id=task_id,
        )
        asyncio.run(batch_export(BatchExportArgs(novel_ids=[local_id]), ctx))

        zip_res = client.get(f"/api/novels/export/{task_id}/download")
        assert zip_res.status_code == 200
        assert zip_res.headers["content-type"].startswith("application/zip")
        assert len(zip_res.content) > 0


def test_disable_pixiv_source_allows_offline_ops_and_blocks_network_sync_and_tasks(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Disabling ``pixiv`` keeps offline ops working and prevents Pixiv network/sync/repair tasks."""
    download_dir = tmp_path / "download"
    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=1001,
                    title="PixivBook1",
                    author_id=2001,
                    author_name="KnownPixivAuthor",
                    series_id=3001,
                    series_name="KnownPixivSeries",
                    series_index=1,
                    tags=["初始标签"],
                    has_epub=EpubStatus.NO_IMAGES,
                    download_dir=str(download_dir),
                ),
                build_novel(
                    id=1002,
                    title="PixivMissing",
                    author_id=2001,
                    author_name="KnownPixivAuthor",
                    series_id=3001,
                    series_name="KnownPixivSeries",
                    series_index=None,
                    has_epub=EpubStatus.MISSING_SOURCE,
                    download_dir=str(download_dir),
                ),
            ],
        )
        pixiv_txt = download_dir / "0000" / "PixivBook1_1001.txt"
        pixiv_txt.parent.mkdir(parents=True, exist_ok=True)
        pixiv_txt.write_text("Pixiv 离线正文内容", encoding="utf-8")
        uow.session.get(models.Novel, 1001).path = str(pixiv_txt)

        FailedNovelRepository(uow.session).record(
            1003,
            "download",
            "timeout",
            title="FailedPixivTitle",
            source=SOURCE_PIXIV,
            source_key="1003",
        )
        uow.session.flush()

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        network_calls: list[tuple[str, Any]] = []

        async def _spy_user_detail(uid: int):
            network_calls.append(("user_detail", uid))
            return {"user": {"id": uid, "name": f"NetUser{uid}"}}

        async def _spy_novel_series(sid: int, fetch_all: bool = False):
            network_calls.append(("novel_series", (sid, fetch_all)))
            return {"series": {"title": f"NetSeries{sid}"}, "novels": []}

        async def _spy_webview_novel(nid: int):
            network_calls.append(("webview_novel", nid))
            return None

        monkeypatch.setattr(app.state.client, "user_detail", _spy_user_detail)
        monkeypatch.setattr(app.state.client, "novel_series", _spy_novel_series)
        monkeypatch.setattr(app.state.client, "webview_novel", _spy_webview_novel)

        # Disable Pixiv source
        dis = client.put("/api/sources/pixiv/config", json={"enabled": False})
        assert dis.status_code == 200
        assert dis.json()["enabled"] is False

        # Offline browsing works
        list_res = client.get("/api/novels")
        assert list_res.status_code == 200
        assert {n["id"] for n in list_res.json()["novels"]} >= {1001, 1002}

        by_ids = client.post("/api/novels/by-ids", json={"novel_ids": [1001]})
        assert by_ids.status_code == 200
        assert by_ids.json()["novels"][0]["title"] == "PixivBook1"

        # Editing allowed fields (tags, series_index) works; read-only fields still 400
        patch_ok = client.patch("/api/novels/1001", json={"tags": ["离线修改"], "series_index": 2})
        assert patch_ok.status_code == 200
        assert patch_ok.json()["tags"] == ["离线修改"]
        assert patch_ok.json()["series_index"] == 2
        assert client.patch("/api/novels/1001", json={"title": "Blocked"}).status_code == 400

        # Download existing Pixiv novel works
        dl = client.get("/api/novels/1001/download")
        assert dl.status_code == 200
        assert "Pixiv 离线正文内容" in dl.text

        # Failed-novels listing works
        failed_res = client.get("/api/failed-novels")
        assert failed_res.status_code == 200
        assert failed_res.json()["total"] == 1
        failed_item = failed_res.json()["items"][0]
        assert failed_item["novel_id"] == 1003
        assert failed_item["external_url"] == "https://www.pixiv.net/novel/show.php?id=1003"
        failed_id = failed_item["id"]

        # Author/Series lookup returns local if known, else unknown WITHOUT calling Pixiv network
        auth_known = client.get("/api/novels/author/2001")
        assert auth_known.status_code == 200
        assert auth_known.json()["source"] == "local"
        assert auth_known.json()["author_name"] == "KnownPixivAuthor"

        auth_unknown = client.get("/api/novels/author/99999")
        assert auth_unknown.status_code == 200
        assert auth_unknown.json()["source"] == "unknown"
        assert auth_unknown.json()["author_name"] is None

        ser_known = client.get("/api/novels/series/3001")
        assert ser_known.status_code == 200
        assert ser_known.json()["source"] == "local"
        assert ser_known.json()["series_name"] == "KnownPixivSeries"

        ser_unknown = client.get("/api/novels/series/88888")
        assert ser_unknown.status_code == 200
        assert ser_unknown.json()["source"] == "unknown"
        assert ser_unknown.json()["series_name"] is None
        assert network_calls == []

        # Single-novel sync rejected with 400 without calling network
        sync_res = client.post("/api/novels/1001/sync")
        assert sync_res.status_code == 400
        assert "已禁用" in sync_res.json()["detail"]
        assert network_calls == []

        # Failed-novels retry endpoint enqueues cleanly, and task skips disabled source without calling client
        retry_http = client.post("/api/failed-novels/retry", json={"ids": [failed_id]})
        assert retry_http.status_code == 200
        assert retry_http.json()["matched"] == 1

        retry_res = asyncio.run(
            retry_failed_novels(
                ids=[failed_id],
                session_factory=committed_session_factory,
                client=app.state.client,
                file_storage=app.state.file_storage,
                image_downloader=None,
                source_service=app.state.source_service,
            )
        )
        assert "跳过 1 条" in retry_res.summary
        assert retry_res.failed_ids == []
        assert network_calls == []

        with pytest.raises(ValidationError, match="已禁用"):
            asyncio.run(
                retry_failed_novels(
                    [1003],
                    session_factory=committed_session_factory,
                    client=app.state.client,
                    file_storage=app.state.file_storage,
                    image_downloader=None,
                    source_service=app.state.source_service,
                )
            )

        # refetch_missing_novels & fix_series_index raise ValidationError when Pixiv is disabled
        with pytest.raises(ValidationError, match="已禁用"):
            asyncio.run(
                refetch_missing_novels(
                    apply=True,
                    uow=SqlUnitOfWork(committed_session_factory),
                    client=app.state.client,
                    file_storage=app.state.file_storage,
                    image_downloader=None,
                    source_service=app.state.source_service,
                )
            )

        with pytest.raises(ValidationError, match="已禁用"):
            asyncio.run(
                fix_series_index(
                    FixSeriesIndexArgs(),
                    TaskContext(
                        uow=SqlUnitOfWork(committed_session_factory),
                        client=app.state.client,
                        source_service=app.state.source_service,
                    ),
                )
            )
        assert network_calls == []

        # Re-enabling Pixiv restores live lookup calls
        assert client.put("/api/sources/pixiv/config", json={"enabled": True}).status_code == 200
        auth_live = client.get("/api/novels/author/99999")
        assert auth_live.status_code == 200
        assert auth_live.json()["source"] == "pixiv"
        assert auth_live.json()["author_name"] == "NetUser99999"
        assert ("user_detail", 99999) in network_calls


def test_pixiv_config_hot_reconfigure_closes_retired_clients_and_updates_pools(
    committed_session_factory,
    tmp_path: Path,
):
    """Updating Pixiv config hot-reconfigures Pool/Client/HttpManager and closes retired clients."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        _create_pixiv_credential(client, name="pool_acc", refresh_token="tok_hot_1234", valid=True)

        http_manager = app.state.http_manager
        pool = app.state.account_pool
        pixiv_client = app.state.client

        client_initial = http_manager.get_client(SOURCE_PIXIV)
        assert client_initial.is_closed is False
        assert len(pool.accounts) == 1
        assert pool.accounts[0]._http_client is client_initial

        # Validation: proxy_mode="custom" without proxy_url -> 400
        bad_proxy = client.put(
            "/api/sources/pixiv/config",
            json={"config": {"proxy_mode": "custom", "proxy_url": "   "}},
        )
        assert bad_proxy.status_code == 400

        # Switch to custom proxy + updated pacing/concurrency
        res_custom = client.put(
            "/api/sources/pixiv/config",
            json={
                "config": {
                    "proxy_mode": "custom",
                    "proxy_url": "127.0.0.1:7890",
                    "min_interval": 4.5,
                    "cooling_duration": 240.0,
                    "max_concurrency": 3,
                }
            },
        )
        assert res_custom.status_code == 200
        cfg_custom = res_custom.json()["config"]
        assert cfg_custom["proxy_mode"] == "custom"
        assert cfg_custom["proxy_url"] == "http://127.0.0.1:7890"
        assert cfg_custom["min_interval"] == 4.5

        client_custom = http_manager.get_client(SOURCE_PIXIV)
        assert client_custom is not client_initial
        assert client_custom.is_closed is False
        assert client_initial.is_closed is True
        assert http_manager._retired == []

        assert pool._http_client is client_custom
        assert pool._min_interval == 4.5
        assert pool._cooling_duration == 240.0
        assert pool.accounts[0]._http_client is client_custom
        assert pool.accounts[0].min_interval == 4.5
        assert pool.accounts[0].cooling_duration == 240.0
        assert http_manager.get_image_downloader(SOURCE_PIXIV)._http_client is client_custom
        assert pixiv_client.max_concurrency == 3

        # Switch to direct mode -> retires & closes custom proxy client cleanly
        res_direct = client.put(
            "/api/sources/pixiv/config",
            json={"config": {"proxy_mode": "direct", "min_interval": 1.5, "max_concurrency": 6}},
        )
        assert res_direct.status_code == 200
        assert res_direct.json()["config"]["proxy_mode"] == "direct"

        client_direct = http_manager.get_client(SOURCE_PIXIV)
        assert client_direct is not client_custom
        assert client_direct.is_closed is False
        assert client_custom.is_closed is True
        assert http_manager._retired == []
        assert pool._http_client is client_direct
        assert pool._min_interval == 1.5
        assert pool.accounts[0].min_interval == 1.5
        assert pixiv_client.max_concurrency == 6

    # Exiting lifespan closes remaining active HTTP client too
    assert client_direct.is_closed is True


def test_source_test_connection_endpoints(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """``POST /api/sources/{source}/test`` enforces capability and returns ``SourceTestResult`` JSON."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Local source does not declare TEST_CONNECTION -> 400
        local_res = client.post("/api/sources/local/test")
        assert local_res.status_code == 400
        assert "不支持连接测试" in local_res.json()["detail"]

        # Unknown source -> 404
        assert client.post("/api/sources/unknown_src/test").status_code == 404

        # Pixiv with no credentials -> SourceTestResult(ok=False)
        no_creds = client.post("/api/sources/pixiv/test")
        assert no_creds.status_code == 200
        assert no_creds.json() == {
            "ok": False,
            "latency_ms": None,
            "message": "未配置有效的 Pixiv 凭据",
            "account_summary": None,
        }

        _create_pixiv_credential(client, name="disabled_acc", refresh_token="tok_dis_0000", valid=False)
        _create_pixiv_credential(client, name="active_acc", refresh_token="tok_act_1111", valid=True)

        auth_responses: list[httpx.Response] = []

        async def _fake_post(_self, url: str, **_kwargs) -> httpx.Response:
            assert str(url) == PIXIV_AUTH_URL
            return auth_responses.pop(0)

        monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

        # Success
        auth_responses.append(
            httpx.Response(
                200, json={"access_token": "acc_ok"}, request=httpx.Request("POST", PIXIV_AUTH_URL)
            )
        )
        ok_res = client.post("/api/sources/pixiv/test")
        assert ok_res.status_code == 200
        ok_body = ok_res.json()
        assert ok_body["ok"] is True
        assert isinstance(ok_body["latency_ms"], int)
        assert "成功" in ok_body["message"]
        assert ok_body["account_summary"] == "active_acc（共 1 个有效凭据）"

        # Invalid token (401)
        auth_responses.append(
            httpx.Response(
                401, json={"error": "invalid_grant"}, request=httpx.Request("POST", PIXIV_AUTH_URL)
            )
        )
        bad_res = client.post("/api/sources/pixiv/test")
        assert bad_res.status_code == 200
        assert bad_res.json()["ok"] is False
        assert "认证失败" in bad_res.json()["message"]
        assert bad_res.json()["account_summary"] == "active_acc"

        # Rate limited (429)
        auth_responses.append(
            httpx.Response(
                429,
                headers={"retry-after": "60"},
                text="rate limit",
                request=httpx.Request("POST", PIXIV_AUTH_URL),
            )
        )
        rl_res = client.post("/api/sources/pixiv/test")
        assert rl_res.status_code == 200
        assert rl_res.json()["ok"] is False
        assert "限流" in rl_res.json()["message"]
        assert rl_res.json()["account_summary"] == "active_acc"


def test_credentials_crud_unique_follow_masking_and_reorder(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Credential CRUD enforces ``unique_meta_flags``, secret masking/preservation, and reorder behavior."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Local source does not support credentials
        local_cred = client.post(
            "/api/sources/local/credentials",
            json={"name": "nope", "secret": {"token": "abc"}},
        )
        assert local_cred.status_code == 400

        # Short secret (<=4 chars) masks completely to "****"
        c1 = _create_pixiv_credential(client, name="short_acc", refresh_token="abcd", is_follow=True)
        assert c1["secret"]["refresh_token"] == "****"
        assert c1["meta"]["is_follow"] is True

        # Second credential with is_follow=True clears sibling's is_follow flag
        c2 = _create_pixiv_credential(
            client, name="long_acc", refresh_token="tok_secret_1234", premium=True, is_follow=True
        )
        assert c2["secret"]["refresh_token"] == "****1234"
        assert c2["meta"]["is_follow"] is True

        listed = client.get("/api/sources/pixiv/credentials").json()
        assert [item["name"] for item in listed] == ["short_acc", "long_acc"]
        assert {item["name"]: item["meta"]["is_follow"] for item in listed} == {
            "short_acc": False,
            "long_acc": True,
        }

        # Empty or masked secret on PUT preserves existing raw refresh_token
        upd_empty = client.put(
            f"/api/sources/pixiv/credentials/{c2['id']}",
            json={"secret": {"refresh_token": ""}},
        )
        assert upd_empty.status_code == 200
        assert upd_empty.json()["secret"]["refresh_token"] == "****1234"

        upd_masked = client.put(
            f"/api/sources/pixiv/credentials/{c2['id']}",
            json={"secret": {"refresh_token": "****9999"}},
        )
        assert upd_masked.status_code == 200
        assert upd_masked.json()["secret"]["refresh_token"] == "****1234"

        with committed_session_factory() as session:
            persisted = session.get(models.SourceCredential, c2["id"])
            assert persisted is not None
            assert persisted.secret["refresh_token"] == "tok_secret_1234"

        # Switching is_follow back + updating secret + toggling valid=False updates AccountPool
        upd_follow = client.put(
            f"/api/sources/pixiv/credentials/{c1['id']}",
            json={"secret": {"refresh_token": "tok_updated_5678"}, "meta": {"is_follow": True}},
        )
        assert upd_follow.status_code == 200
        assert upd_follow.json()["secret"]["refresh_token"] == "****5678"
        assert {a.username: a.token_info.follow for a in app.state.account_pool.accounts} == {
            "short_acc": True,
            "long_acc": False,
        }

        # Reorder changes sort_index without calling AccountPool.reload_from_tokens
        reloads: list[Any] = []
        monkeypatch.setattr(
            app.state.account_pool, "reload_from_tokens", lambda tokens: reloads.append(tokens)
        )
        reord = client.post(
            "/api/sources/pixiv/credentials/reorder",
            json=[c2["id"], c1["id"]],
        )
        assert reord.status_code == 200
        assert reord.json() == {"ok": True}
        assert reloads == []
        monkeypatch.undo()

        reordered_list = client.get("/api/sources/pixiv/credentials").json()
        assert [(item["id"], item["sort_index"]) for item in reordered_list] == [
            (c2["id"], 0),
            (c1["id"], 1),
        ]

        # Toggling valid=False excludes account from AccountPool while keeping DB credential
        disable_cred = client.put(
            f"/api/sources/pixiv/credentials/{c1['id']}",
            json={"valid": False},
        )
        assert disable_cred.status_code == 200
        assert disable_cred.json()["valid"] is False
        assert [a.username for a in app.state.account_pool.accounts] == ["long_acc"]

        # Delete credential removes it from DB and AccountPool
        assert client.delete(f"/api/sources/pixiv/credentials/{c1['id']}").status_code == 204
        assert [a.username for a in app.state.account_pool.accounts] == ["long_acc"]


# ---------------------------------------------------------------------------
# 2. Direct DB + service tests against PostgreSQL
# ---------------------------------------------------------------------------


def test_entity_resolver_pixiv_upsert_and_external_sequence_allocation(uow: SqlUnitOfWork):
    """``EntityResolver`` upserts positive Pixiv IDs and allocates idempotent negative external IDs."""
    with uow.begin():
        resolver = EntityResolver(uow.session)

        # Pixiv author & series upsert (> 0)
        aid_p = resolver.resolve_author(SOURCE_PIXIV, "42001", name="PixivAuthor")
        assert aid_p == 42001
        assert resolver.resolve_author(SOURCE_PIXIV, "42001", name="PixivAuthorRenamed") == 42001
        author_p = uow.session.get(models.Author, 42001)
        assert author_p is not None
        assert author_p.author_name == "PixivAuthorRenamed"
        assert author_p.source == SOURCE_PIXIV
        assert author_p.source_key == "42001"

        sid_p = resolver.resolve_series(SOURCE_PIXIV, "84001", name="PixivSeries", author_id=aid_p)
        assert sid_p == 84001
        assert resolver.resolve_series(SOURCE_PIXIV, "84001", name="PixivSeriesV2", author_id=aid_p) == 84001
        series_p = uow.session.get(models.Series, 84001)
        assert series_p is not None
        assert series_p.series_name == "PixivSeriesV2"

        # Non-Pixiv external author (< 0, idempotent on repeat (source, source_key))
        aid_e1 = resolver.resolve_author("esj", "ext_author_1", name="ESJAuthor1")
        assert aid_e1 < 0
        assert resolver.resolve_author("esj", "ext_author_1", name="ESJAuthor1Updated") == aid_e1
        aid_e2 = resolver.resolve_author("esj", "ext_author_2", name="ESJAuthor2")
        assert aid_e2 < 0
        assert aid_e2 != aid_e1
        assert uow.session.get(models.Author, aid_e1).author_name == "ESJAuthor1Updated"

        # Non-Pixiv external series (< 0, idempotent on repeat (source, source_key))
        sid_e1 = resolver.resolve_series("esj", "ext_series_1", name="ESJSeries1", author_id=aid_e1)
        assert sid_e1 < 0
        assert resolver.resolve_series("esj", "ext_series_1", name="ESJSeries1V2", author_id=aid_e1) == sid_e1
        sid_e2 = resolver.resolve_series("esj", "ext_series_2", name="ESJSeries2", author_id=aid_e2)
        assert sid_e2 < 0
        assert sid_e2 != sid_e1
        assert uow.session.get(models.Series, sid_e1).series_name == "ESJSeries1V2"

        # Negative timeline coordinates for external novels
        nid1 = resolver.next_novel_id()
        nid2 = resolver.next_novel_id()
        assert nid2 < nid1 < 0


def test_tag_alias_resolver_chain_cycle_and_dedup_normalization(uow: SqlUnitOfWork):
    """``TagAliasResolver`` normalizes/deduplicates tags and safely resolves single-hop chains and cycles."""
    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        tag_repo.create_alias({"source": "alias_a", "target": "canon"})
        tag_repo.create_alias({"source": "alias_b", "target": "canon"})

        # Repo guards prevent creating chains/cycles via API
        tag_repo.create_alias({"source": "guard_a", "target": "guard_b"})
        with pytest.raises(ValidationError, match="循环别名"):
            tag_repo.create_alias({"source": "guard_b", "target": "guard_a"})
        with pytest.raises(ValidationError, match="多级别名"):
            tag_repo.create_alias({"source": "guard_b", "target": "guard_c"})

        # Seed raw chain & cycle rows in DB to verify TagAliasResolver handles them deterministically
        uow.session.add_all(
            [
                models.TagAlias(source="step1", target="step2"),
                models.TagAlias(source="step2", target="step3"),
                models.TagAlias(source="cyc_x", target="cyc_y"),
                models.TagAlias(source="cyc_y", target="cyc_x"),
            ]
        )
        uow.session.flush()

        resolver = TagAliasResolver(uow.session)

        # Compound splitting + case folding + alias mapping + deduplication
        assert resolver.resolve(["ALIAS_A", "alias_b/Canon", "(extra)"]) == ["canon", "extra"]
        assert resolver.resolve(["(extra)", "alias_b", "alias_a"], sort=False) == ["extra", "canon"]
        assert resolver.resolve([{"name": "alias_a"}, {"name": "canon"}]) == ["canon"]

        # Single-hop chain lookup (does not cascade step1 -> step3)
        assert resolver.resolve(["step1"]) == ["step2"]
        assert resolver.resolve(["step2"]) == ["step3"]

        # Cycle terminates immediately in single hop
        assert resolver.resolve(["cyc_x", "cyc_y"]) == ["cyc_x", "cyc_y"]
        assert resolver.resolve_set({"alias_a", "alias_b", "cyc_x", "plain"}) == {
            "canon",
            "cyc_y",
            "plain",
        }


@pytest.mark.asyncio
async def test_fetch_special_follow_novels_and_author_special_follow_dispatch(
    committed_session_factory,
    tmp_path: Path,
):
    """``fetch_special_follow_novels`` and ``author_special_follow`` dispatch via ``SourceService``."""
    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add_all(
            [
                models.Author(
                    author_id=501,
                    author_name="FollowPixiv",
                    is_special_follow=True,
                    source=SOURCE_PIXIV,
                    source_key="501",
                ),
                models.Author(
                    author_id=-502,
                    author_name="FollowLocal",
                    is_special_follow=True,
                    source=SOURCE_LOCAL,
                    source_key="-502",
                ),
                models.Author(
                    author_id=503,
                    author_name="NotFollowed",
                    is_special_follow=False,
                    source=SOURCE_PIXIV,
                    source_key="503",
                ),
            ]
        )

    fake_client = _RecordingPixivClient()
    fake_client.user_novels_responses[501] = {
        "novels": [
            _make_novel_info(
                5011, title="中文特别关注新作", author_id=501, author_name="FollowPixiv", tags=["中文"]
            ),
            _make_novel_info(
                5012, title="これは日本語です", author_id=501, author_name="FollowPixiv", caption="日本語"
            ),
        ]
    }
    fake_client.webview_responses[5011] = _make_webview_novel(
        5011,
        title="中文特别关注新作",
        author_id=501,
        text="特别关注拉取的正文",
    )

    http_manager = SourceHttpManager()
    registry = SourceRegistry(
        [PixivSourcePlugin(http_manager=http_manager, client=fake_client), LocalSourcePlugin()]
    )
    source_service = SourceService(committed_session_factory, registry, http_manager)
    source_service.bootstrap()
    storage = FileStorage(str(tmp_path / "download"))
    image_downloader = NovelAssetPipeline(
        http_manager.get_image_downloader(SOURCE_PIXIV),
        download_root=storage.download_dir,
    )

    try:
        res = await fetch_special_follow_novels(
            uow=uow,
            client=fake_client,
            file_storage=storage,
            image_downloader=image_downloader,
            source_service=source_service,
        )
        assert "新增 1 本小说" in res.summary
        assert ("user_novels", (501, False)) in fake_client.calls
        assert ("user_novels", (503, False)) not in fake_client.calls

        with committed_session_factory() as session:
            persisted = session.get(models.Novel, 5011)
            assert persisted is not None
            assert persisted.title == "中文特别关注新作"
            assert session.get(models.Novel, 5012) is None

        # Task adapter ``author_special_follow`` dispatches through the same SourceService path
        fake_client.calls.clear()
        ctx = TaskContext(
            uow=uow,
            session_factory=committed_session_factory,
            client=fake_client,
            file_storage=storage,
            image_downloader=image_downloader,
            source_registry=registry,
            source_service=source_service,
        )
        task_res = await author_special_follow(ctx)
        assert ("user_novels", (501, False)) in fake_client.calls
        assert "特别关注" in task_res.summary

        # Disabling Pixiv causes fetch_special_follow_novels to skip Pixiv cleanly
        source_service.update_config(uow, SOURCE_PIXIV, enabled=False)
        fake_client.calls.clear()
        disabled_res = await fetch_special_follow_novels(
            uow=uow,
            client=fake_client,
            file_storage=storage,
            image_downloader=image_downloader,
            source_service=source_service,
        )
        assert fake_client.calls == []
        assert "新增 0 本小说" in disabled_res.summary
    finally:
        await image_downloader.aclose()
        await http_manager.aclose()


@pytest.mark.asyncio
async def test_delete_author_invokes_on_author_deleted_for_enabled_sources(
    committed_session_factory,
    tmp_path: Path,
):
    """``delete_author`` invokes ``on_author_deleted`` only when the author's source is enabled."""
    download_dir = tmp_path / "download"
    storage = FileStorage(str(download_dir))
    uow = SqlUnitOfWork(committed_session_factory)

    pixiv_file_1 = download_dir / "0000" / "Author701_7011.txt"
    pixiv_file_1.parent.mkdir(parents=True, exist_ok=True)
    pixiv_file_1.write_text("p1", encoding="utf-8")

    pixiv_file_2 = download_dir / "0000" / "Author702_7021.txt"
    pixiv_file_2.write_text("p2", encoding="utf-8")

    local_file = download_dir / "local" / "Author703_-7031.txt"
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_text("l1", encoding="utf-8")

    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=7011,
                    title="Author701",
                    author_id=701,
                    author_name="A701",
                    path=str(pixiv_file_1),
                ),
                build_novel(
                    id=7021,
                    title="Author702",
                    author_id=702,
                    author_name="A702",
                    path=str(pixiv_file_2),
                ),
            ],
        )
        uow.session.add(
            models.Author(
                author_id=-703,
                author_name="LocalAuthor703",
                source=SOURCE_LOCAL,
                source_key="-703",
            )
        )
        uow.session.flush()
        uow.session.add(
            make_local_novel(
                -7031,
                title="Author703",
                author_id=-703,
                author_name="LocalAuthor703",
                path=str(local_file),
            )
        )
        uow.session.flush()

    fake_client = _RecordingPixivClient()
    http_manager = SourceHttpManager()
    registry = SourceRegistry(
        [PixivSourcePlugin(http_manager=http_manager, client=fake_client), LocalSourcePlugin()]
    )
    source_service = SourceService(committed_session_factory, registry, http_manager)
    source_service.bootstrap()

    try:
        # 1. Enabled Pixiv source -> deletes DB rows + files AND invokes on_author_deleted
        await delete_author(
            701,
            uow=uow,
            file_storage=storage,
            client=fake_client,
            source_service=source_service,
        )
        assert ("user_follow_delete", 701) in fake_client.calls
        assert {"force_follow": True} in fake_client.follow_rules
        assert not pixiv_file_1.exists()
        with committed_session_factory() as session:
            assert session.get(models.Author, 701) is None
            assert session.get(models.Novel, 7011) is None

        # 2. Local source (enabled, no-op hook) -> deletes local author + files cleanly
        fake_client.calls.clear()
        await delete_author(
            -703,
            uow=uow,
            file_storage=storage,
            client=fake_client,
            source_service=source_service,
        )
        assert fake_client.calls == []
        assert not local_file.exists()
        with committed_session_factory() as session:
            assert session.get(models.Author, -703) is None
            assert session.get(models.Novel, -7031) is None

        # 3. Disabled Pixiv source -> still deletes DB rows + files, skips on_author_deleted hook
        source_service.update_config(uow, SOURCE_PIXIV, enabled=False)
        fake_client.calls.clear()
        await delete_author(
            702,
            uow=uow,
            file_storage=storage,
            client=fake_client,
            source_service=source_service,
        )
        assert fake_client.calls == []
        assert not pixiv_file_2.exists()
        with committed_session_factory() as session:
            assert (
                session.execute(
                    select(models.Author).where(models.Author.author_id == 702)
                ).scalar_one_or_none()
                is None
            )
            assert session.get(models.Novel, 7021) is None
    finally:
        await http_manager.aclose()
