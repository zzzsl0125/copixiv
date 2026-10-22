"""Endpoint smoke tests — novels / tasks / search-history via TestClient.

Builds a minimal FastAPI app with the same wiring as
``container.create_app()`` (routers + DomainError handler + app.state
dependencies) but with a temp-file SQLite DB and fakes for out-of-process
deps (task manager, pixiv client, file storage).

Pins the v1-compatible wire contract:
- novel list: ``{novels, cursor}`` shape, int flags (is_favourite /
  is_special_follow / has_epub), tags arrays
- batch-download: streaming ZIP response + Content-Disposition +
  X-Batch-Missing-Ids
- DomainError → HTTP status + ``{"detail": ...}`` body
- search-history: list / delete-one / clear-all
"""

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError, NotFoundError
from copixiv.db.models import (
    Author, Novel, SearchHistory, Tag, TaskHistory,
)
from copixiv.tasks import api as tasks
from copixiv.features.novels import api as novels
from copixiv.features.novels import history_api as search_history
from copixiv.features.tags import aliases as tag_aliases
from copixiv.features.tags import preferences as tag_preferences
from copixiv.features.system import api as system


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


class _FakeFileStorage:
    def __init__(self, download_dir: Path):
        self.download_dir = str(download_dir)

    def delete_novel_files(self, novel_path: str) -> None:
        p = Path(novel_path)
        p.unlink(missing_ok=True)
        p.with_suffix(".epub").unlink(missing_ok=True)


class _FakeTaskManager:
    """Records calls; run_task_now raises for unknown ids like the real one."""

    def __init__(self):
        self.reloads = 0
        self.runs: list[int] = []

    def reload_cron_jobs(self) -> None:
        self.reloads += 1

    def run_task_now(self, task_id: int) -> None:
        self.runs.append(task_id)
        if task_id == 999:
            # 真实实现抛 NotFoundError（DomainError → 404）
            raise NotFoundError("no such scheduled task")


# session_factory comes from tests/conftest.py (shared in-memory engine).

@pytest.fixture
def client(session_factory, tmp_path):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = AppConfig()
    app.state.file_storage = _FakeFileStorage(tmp_path)
    app.state.task_manager = _FakeTaskManager()

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
    app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(
        search_history.router, prefix="/api/search-history",
        tags=["search_history"],
    )
    app.include_router(
        tag_preferences.router, prefix="/api/tag-preferences",
        tags=["tag_preferences"],
    )
    app.include_router(
        tag_aliases.router, prefix="/api/tag-aliases", tags=["tag_aliases"],
    )
    app.include_router(system.router, prefix="/api/system", tags=["system"])

    with TestClient(app) as c:
        yield c


def _seed_novel(sf, novel_id: int, title: str, path: str, **extra):
    with sf() as s:
        s.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
        s.flush()
        s.add(Novel(
            id=novel_id, title=title, author_id=novel_id,
            author_name=f"作者{novel_id}", path=path,
            series_name="系列", series_index=1, has_epub=0,
            **extra,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# GET /api/novels/
# ---------------------------------------------------------------------------


class TestNovelsList:
    def test_list_shape_and_int_flags(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题一", str(tmp_path / "1.txt"))
        _seed_novel(session_factory, 2, "标题二", str(tmp_path / "2.txt"))

        r = client.get("/api/novels/", params={"order_by": "id", "per_page": 20})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"novels", "cursor", "has_excluded"}
        # 无关键词浏览不计算 has_excluded，保持默认 False
        assert body["has_excluded"] is False
        assert [n["id"] for n in body["novels"]] == [2, 1]  # DESC default
        novel = body["novels"][0]
        # v1-compatible wire format: ints, not JSON booleans
        assert novel["is_favourite"] == 0
        assert novel["is_special_follow"] == 0
        assert novel["has_epub"] == 0
        # f453b20 regression guard: the response_model must keep author_id,
        # otherwise the frontend author-search / special-follow buttons break.
        assert novel["author_id"] == 2
        assert [n["author_id"] for n in body["novels"]] == [2, 1]
        assert isinstance(novel["tags"], list)

    def test_cursor_pagination_roundtrip(self, client, session_factory, tmp_path):
        """走完所有页 = 全集，且不重不漏。

        游标取**本页最后一行**。早前取的是多取的那行「探测行」（id=3），而游标
        比较是严格 ``<``，于是探测行自己在下一页被排除 —— 每翻一页静默丢一本。
        """
        for i in range(1, 6):
            _seed_novel(session_factory, i, f"标题{i}", str(tmp_path / f"{i}.txt"))

        seen: list[int] = []
        cursor = None
        for _ in range(4):
            body = client.get(
                "/api/novels/",
                params={
                    "order_by": "id", "per_page": 2,
                    **({"cursor": json.dumps(cursor)} if cursor else {}),
                },
            ).json()
            seen.extend(n["id"] for n in body["novels"])
            cursor = body["cursor"]
            if cursor is None:
                break

        assert seen == [5, 4, 3, 2, 1]

    def test_toggle_favourite_and_filter(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题", str(tmp_path / "1.txt"))
        r = client.post("/api/novels/1/favourite")
        assert r.status_code == 204

        r = client.get(
            "/api/novels/",
            params={
                "order_by": "id",
                "keyword": "is_favourite:true",
            },
        )
        assert [n["id"] for n in r.json()["novels"]] == [1]
        assert r.json()["novels"][0]["is_favourite"] == 1

    def test_count(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题", str(tmp_path / "1.txt"))
        r = client.get("/api/novels/count")
        assert r.status_code == 200
        assert r.json() == {
            "total": 1, "excluded": 0,
            "truncated": False, "excluded_truncated": False,
        }

    def test_count_over_cap_is_flagged(
        self, client, session_factory, tmp_path, monkeypatch,
    ):
        """命中数 > COUNT_CAP：total 封顶为 cap、truncated=true（下界）。

        老客户端读到的 ``total`` 仍是数字（语义从「精确」变成「至少」），
        新客户端看 ``truncated`` 决定显示 ``N+``。
        """
        from copixiv.features.novels import repo as repo_module
        monkeypatch.setattr(repo_module, "COUNT_CAP", 2)
        for i in (1, 2, 3):
            _seed_novel(session_factory, i, f"标题{i}", str(tmp_path / f"{i}.txt"))

        r = client.get("/api/novels/count")
        assert r.status_code == 200
        assert r.json() == {
            "total": 2, "excluded": 0,
            "truncated": True, "excluded_truncated": False,
        }

    def test_unknown_condition_type_is_400(self, client):
        """A typo in a condition type must be loud, not an empty result."""
        r = client.get("/api/novels/", params={"keyword": "foo:bar"})
        assert r.status_code == 400
        assert "Invalid query field: foo" in r.json()["detail"]

    def test_keyword_condition_filters_and_records_history(
        self, client, session_factory, tmp_path,
    ):
        _seed_novel(session_factory, 1, "标题一", str(tmp_path / "1.txt"))
        _seed_novel(session_factory, 2, "标题二", str(tmp_path / "2.txt"))

        # A field condition narrows the result set (no FTS table needed).
        r = client.get("/api/novels/", params={"keyword": "author_id:1"})
        assert r.status_code == 200
        assert [n["id"] for n in r.json()["novels"]] == [1]

        # BackgroundTasks run before the response returns → the search is
        # recorded with the author's display name resolved.
        history = client.get("/api/search-history/").json()
        entry = next(h for h in history if h["type"] == "author_id")
        assert entry["value"] == "1"
        assert entry["display_value"] == "作者1"


# ---------------------------------------------------------------------------
# POST /api/novels/batch-download
# ---------------------------------------------------------------------------


class TestBatchDownload:
    def test_streams_zip_with_headers(self, client, session_factory, tmp_path):
        for i in (1, 2):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed_novel(session_factory, i, f"标题{i}", str(p))
        # A novel whose file is missing → reported via header, not in ZIP
        _seed_novel(session_factory, 3, "标题三", str(tmp_path / "missing.txt"))

        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "order_direction": "ASC", "limit": 10,
            "format_mode": "txt",
        })
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")
        assert "filename*=UTF-8''" in r.headers["content-disposition"]
        assert r.headers.get("x-batch-missing-ids") == "3"

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(zf.namelist())
        assert names == ["作者1/系列/#1_标题1_1.txt", "作者2/系列/#1_标题2_2.txt"]
        assert zf.read("作者1/系列/#1_标题1_1.txt").decode() == "正文1"

    def test_accepts_limit_500(self, client, session_factory, tmp_path):
        p = tmp_path / "1.txt"
        p.write_text("正文", encoding="utf-8")
        _seed_novel(session_factory, 1, "标题", str(p))

        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "limit": 500, "format_mode": "txt",
        })
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")

    def test_empty_match_maps_to_404(self, client):
        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "min_like": 99999999,
        })
        assert r.status_code == 404
        assert r.json() == {"detail": "未找到匹配条件的小说"}

    def test_preview(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题一", str(tmp_path / "1.txt"))
        r = client.post("/api/novels/batch-download/preview", json={
            "order_by": "id", "naming_template": "{author_name}/{title}_{id}",
        })
        assert r.status_code == 200
        assert r.json() == {"path": "作者1/标题一_1.txt"}


# ---------------------------------------------------------------------------
# Tasks endpoints
# ---------------------------------------------------------------------------


class TestTasksEndpoints:
    def test_methods_include_maintenance_tasks(self, client):
        r = client.get("/api/tasks/methods")
        assert r.status_code == 200
        methods = {m["name"]: m for m in r.json()}
        assert {"novel_fetch", "rebuild_fts", "check_fts"} <= set(methods)

        # Arguments are Pydantic's JSON Schema; parameter-less tasks send null.
        search = methods["novel_search"]["arguments"]
        assert search["properties"]["keyword"]["type"] == "string"
        assert search["properties"]["keyword"]["default"] == "R-18"
        assert methods["novel_fetch"]["arguments"]["required"] == ["id"]
        assert methods["rebuild_fts"]["arguments"] is None

    def test_history_empty(self, client):
        r = client.get("/api/tasks/history")
        assert r.status_code == 200
        assert r.json() == {"items": [], "total": 0}

    def test_create_scheduled_reloads_cron_jobs(self, client, session_factory):
        r = client.post("/api/tasks/scheduled", json={
            "name": "测试", "task": "rebuild_fts", "cron": "0 4 * * 1",
            "is_enabled": False,
        })
        assert r.status_code == 200
        task = r.json()
        assert task["id"] > 0
        assert task["task"] == "rebuild_fts"
        assert client.app.state.task_manager.reloads == 1

    def test_create_scheduled_accepts_valid_five_field_cron(self, client):
        r = client.post("/api/tasks/scheduled", json={
            "name": "工作日", "task": "rebuild_fts",
            "cron": "*/15 9-17 * * 1-5", "is_enabled": False,
        })
        assert r.status_code == 200
        assert r.json()["cron"] == "*/15 9-17 * * 1-5"

    def test_scheduled_tasks_no_longer_expose_config(self, client):
        """``config`` was a dead TEXT column; the wire shape must not carry it.

        The UI never had an input for it and always sent ``{}``; ``params``
        (JSONB) is the field tasks actually read.  Pinning this keeps the
        generated frontend types from silently growing the field back.
        """
        created = client.post("/api/tasks/scheduled", json={
            "name": "无 config", "task": "rebuild_fts", "cron": "0 4 * * *",
            "is_enabled": False, "config": {"ignored": True},
        })
        assert created.status_code == 200
        assert "config" not in created.json()

        listed = client.get("/api/tasks/scheduled")
        assert listed.status_code == 200
        assert listed.json()
        assert all("config" not in t for t in listed.json())

        task_id = created.json()["id"]
        updated = client.put(f"/api/tasks/scheduled/{task_id}", json={
            "name": "改名后", "is_enabled": True,
        })
        assert updated.status_code == 200
        assert updated.json()["name"] == "改名后"
        assert "config" not in updated.json()

    @pytest.mark.parametrize(
        "cron",
        [
            "not-a-cron",
            "0 3 * *",        # only 4 fields
            "0 0 3 * * *",    # 6 fields (croniter would accept seconds)
            "60 3 * * *",     # minute out of range
        ],
    )
    def test_create_scheduled_rejects_invalid_cron(self, client, cron):
        r = client.post("/api/tasks/scheduled", json={
            "name": "坏 cron", "task": "rebuild_fts", "cron": cron,
            "is_enabled": False,
        })
        assert r.status_code == 422
        assert client.app.state.task_manager.reloads == 0

    def test_busy_endpoint_returns_distinct_running_functions(
        self, client, session_factory,
    ):
        """The busy set is computed server-side (one tiny query)."""
        from datetime import datetime, timezone

        with session_factory() as session:
            for name, func, status in (
                ("a", "batch_export", "pending"),
                ("b", "batch_export", "running"),
                ("c", "check_epub", "running"),
                ("d", "failed_retry", "success"),      # terminal → excluded
                ("e", None, "pending"),                # legacy, unattributable
            ):
                session.add(TaskHistory(
                    name=name, task_func=func, arguments="{}", status=status,
                    start_time=datetime.now(timezone.utc),
                ))
            session.commit()

        r = client.get("/api/tasks/busy")
        assert r.status_code == 200
        assert r.json()["funcs"] == ["batch_export", "check_epub"]

    def test_history_list_omits_log_and_flags_it(self, client, session_factory):
        """The list must not inline log text; ``has_log`` says where to get it."""
        from datetime import datetime, timezone

        with session_factory() as session:
            row = TaskHistory(
                name="withlog", task_func="check_epub", arguments="{}",
                status="success", start_time=datetime.now(timezone.utc),
                result={"summary": "done", "log": "x" * 5000},
            )
            session.add(row)
            session.commit()
            task_id = row.id

        r = client.get("/api/tasks/history?limit=10")
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["id"] == task_id)
        assert item["has_log"] is True
        assert "log" not in (item["result"] or {})
        assert item["result"]["summary"] == "done"

        log = client.get(f"/api/tasks/history/{task_id}/log")
        assert log.status_code == 200
        assert log.json()["log"] == "x" * 5000

    def test_history_log_404_for_row_without_log(self, client, session_factory):
        from datetime import datetime, timezone

        with session_factory() as session:
            row = TaskHistory(
                name="nolog", task_func="check_epub", arguments="{}",
                status="success", start_time=datetime.now(timezone.utc),
                result={"summary": "done"},
            )
            session.add(row)
            session.commit()
            task_id = row.id

        assert client.get(f"/api/tasks/history/{task_id}/log").status_code == 404

    def test_create_scheduled_rejects_params_that_fail_the_args_model(self, client):
        """Params are validated against the task's Pydantic model at write time.

        Production had a ``novel_fetch`` schedule stored with ``{"id": null}``:
        the API accepted it and the failure only surfaced when the cron fired.
        """
        r = client.post("/api/tasks/scheduled", json={
            "name": "bad params", "task": "novel_fetch", "cron": "0 0 * * *",
            "params": {"id": None},
        })
        assert r.status_code == 400, r.text
        assert "参数无效" in r.json()["detail"]

    def test_create_scheduled_accepts_valid_params(self, client):
        r = client.post("/api/tasks/scheduled", json={
            "name": "ok params", "task": "novel_fetch", "cron": "0 0 * * *",
            "params": {"id": 123},
        })
        assert r.status_code == 200, r.text

    def test_update_scheduled_rejects_params_for_the_stored_task(self, client):
        created = client.post("/api/tasks/scheduled", json={
            "name": "upd", "task": "novel_fetch", "cron": "0 0 * * *",
            "params": {"id": 123},
        })
        assert created.status_code == 200
        task_id = created.json()["id"]
        r = client.put(f"/api/tasks/scheduled/{task_id}", json={
            "params": {"id": None},
        })
        assert r.status_code == 400, r.text

    def test_update_scheduled_rejects_invalid_cron(self, client):
        created = client.post("/api/tasks/scheduled", json={
            "name": "测试", "task": "rebuild_fts", "cron": "0 4 * * 1",
            "is_enabled": False,
        })
        assert created.status_code == 200
        task_id = created.json()["id"]

        r = client.put(f"/api/tasks/scheduled/{task_id}", json={
            "cron": "0 0 3 * * *",
        })
        assert r.status_code == 422
        assert client.app.state.task_manager.reloads == 1

    def test_run_unknown_task_maps_to_404(self, client):
        r = client.post("/api/tasks/scheduled/999/run")
        assert r.status_code == 404
        assert "detail" in r.json()


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------


class TestSearchHistory:
    def test_clear_all(self, client, session_factory):
        with session_factory() as s:
            s.add(SearchHistory(type="keyword", value="R-18",
                                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)))
            s.add(SearchHistory(type="author_id", value="123",
                                timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc)))
            s.commit()

        r = client.get("/api/search-history/")
        assert r.status_code == 200
        assert len(r.json()) == 2
        item = r.json()[0]
        assert {"id", "type", "value", "display_value", "timestamp"} <= set(item)

        r = client.delete("/api/search-history/")
        assert r.status_code == 200
        assert r.json() == {"deleted": 2}
        assert client.get("/api/search-history/").json() == []

    def test_delete_missing_item_maps_to_404(self, client):
        r = client.delete("/api/search-history/999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# DomainError mapping (all use cases share the same handler)
# ---------------------------------------------------------------------------


class TestDomainErrorMapping:
    def test_not_found_is_json_404(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题", str(tmp_path / "1.txt"))
        r = client.delete("/api/novels/777")
        assert r.status_code == 404
        assert "detail" in r.json()
        # And the shared handler maps NotFoundError to 404
        assert _domain_error_http_status(NotFoundError("x")) == 404


# ---------------------------------------------------------------------------
# Wire-contract bounds: batch-download limit / novel per_page
# ---------------------------------------------------------------------------


class TestRequestBounds:
    def test_batch_download_limit_500_accepted(self, client, session_factory, tmp_path):
        p = tmp_path / "1.txt"
        p.write_text("正文", encoding="utf-8")
        _seed_novel(session_factory, 1, "标题", str(p))

        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "limit": 500, "format_mode": "txt",
        })
        assert r.status_code == 200

    def test_batch_download_limit_above_500_accepted(self, client, session_factory, tmp_path):
        # 选多少下多少 — the old 500-per-zip cap is gone; large limits
        # are the caller's explicit choice.
        p = tmp_path / "1.txt"
        p.write_text("正文", encoding="utf-8")
        _seed_novel(session_factory, 1, "标题", str(p))

        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "limit": 1000, "format_mode": "txt",
        })
        assert r.status_code == 200

    def test_batch_download_limit_zero_rejected(self, client):
        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "limit": 0, "format_mode": "txt",
        })
        assert r.status_code == 422

    def test_per_page_200_accepted(self, client):
        r = client.get("/api/novels/", params={"per_page": 200})
        assert r.status_code == 200

    def test_per_page_201_rejected(self, client):
        r = client.get("/api/novels/", params={"per_page": 201})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/novels/{id} success path: DB row + files + tag counts
# ---------------------------------------------------------------------------


class TestDeleteNovelSuccess:
    def test_delete_removes_row_files_and_tag_reference(
        self, client, session_factory, tmp_path,
    ):
        novel_path = tmp_path / "1.txt"
        novel_path.write_text("正文", encoding="utf-8")
        epub_path = tmp_path / "1.epub"
        epub_path.write_bytes(b"epub")

        with session_factory() as s:
            s.add(Author(author_id=1, author_name="作者1"))
            s.flush()
            s.add(Novel(
                id=1, title="标题", author_id=1, author_name="作者1",
                path=str(novel_path), tags=["R-18"],
            ))
            s.commit()

        r = client.delete("/api/novels/1")
        assert r.status_code == 204

        assert not novel_path.exists(), "txt file must be deleted"
        assert not epub_path.exists(), "epub file must be deleted"

        with session_factory() as s:
            assert s.get(Novel, 1) is None
            tag = s.query(Tag).filter_by(name="R-18").one()
            assert tag.reference_count == 0, (
                f"tag.reference_count should drop to 0, got {tag.reference_count}"
            )


# ---------------------------------------------------------------------------
# GET /api/novels/{id}/download — file serving + path-escape guard
# ---------------------------------------------------------------------------


class TestNovelFileDownload:
    def test_download_txt(self, client, session_factory, tmp_path):
        novel_path = tmp_path / "1.txt"
        novel_path.write_text("正文内容", encoding="utf-8")
        _seed_novel(session_factory, 1, "标题", str(novel_path))

        r = client.get("/api/novels/1/download")
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "正文内容"

    def test_download_epub(self, client, session_factory, tmp_path):
        novel_path = tmp_path / "1.txt"
        novel_path.write_text("正文", encoding="utf-8")
        (tmp_path / "1.epub").write_bytes(b"epub-bytes")
        _seed_novel(session_factory, 1, "标题", str(novel_path))

        r = client.get("/api/novels/1/download", params={"format": "epub"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/epub+zip"

    def test_download_missing_file_is_404(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题", str(tmp_path / "missing.txt"))
        r = client.get("/api/novels/1/download")
        assert r.status_code == 404

    def test_download_path_escaping_root_is_404(self, client, session_factory):
        _seed_novel(session_factory, 1, "标题", "/etc/passwd")
        r = client.get("/api/novels/1/download")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tag preferences / aliases / system config (previously 0% covered)
# ---------------------------------------------------------------------------


class TestSystemConfigEndpoint:
    def test_config_shape(self, client):
        r = client.get("/api/system/config")
        assert r.status_code == 200
        assert set(r.json()) == {
            "batch_download_naming", "exclude_blocked_tag_novels",
        }
        assert r.json()["exclude_blocked_tag_novels"] is True

    def test_update_config_persists(self, client):
        """PUT toggles the runtime setting (settings table, default-on)."""
        r = client.put("/api/system/config", json={
            "exclude_blocked_tag_novels": False,
        })
        assert r.status_code == 200
        assert r.json()["exclude_blocked_tag_novels"] is False

        r = client.get("/api/system/config")
        assert r.json()["exclude_blocked_tag_novels"] is False

        # Back to default-on
        r = client.put("/api/system/config", json={
            "exclude_blocked_tag_novels": True,
        })
        assert r.json()["exclude_blocked_tag_novels"] is True


class TestTagPreferencesEndpoints:
    def test_crud_and_reorder(self, client):
        r = client.post("/api/tag-preferences/", json={
            "tag": "NTR", "preference": "blocked", "sort_index": 0,
        })
        assert r.status_code == 200
        pref_id = r.json()["id"]

        r = client.post("/api/tag-preferences/", json={
            "tag": "R-18", "preference": "favourite", "sort_index": 1,
        })
        assert r.status_code == 200
        pref2_id = r.json()["id"]

        r = client.get("/api/tag-preferences/")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert {p["tag"] for p in body} == {"NTR", "R-18"}

        r = client.put(f"/api/tag-preferences/{pref_id}", json={
            "preference": "favourite",
        })
        assert r.status_code == 200
        assert r.json()["preference"] == "favourite"

        r = client.post("/api/tag-preferences/reorder", json=[pref2_id, pref_id])
        assert r.status_code == 200

        r = client.delete(f"/api/tag-preferences/{pref_id}")
        assert r.status_code == 200
        assert len(client.get("/api/tag-preferences/").json()) == 1

    def test_delete_missing_maps_to_404(self, client):
        r = client.delete("/api/tag-preferences/999")
        assert r.status_code == 404


class TestTagAliasEndpoints:
    def test_create_list_suggest_delete(self, client):
        r = client.post("/api/tag-aliases/", json={
            "source": "R-18", "target": "R18",
        })
        assert r.status_code == 200
        alias_id = r.json()["id"]

        r = client.get("/api/tag-aliases/")
        assert r.status_code == 200
        assert {"source": "R-18", "target": "R18"} in [
            {"source": a["source"], "target": a["target"]} for a in r.json()
        ]

        r = client.get("/api/tag-aliases/suggest", params={"limit": 10})
        assert r.status_code == 200
        assert set(r.json()) == {"items", "next_offset"}

        r = client.delete(f"/api/tag-aliases/{alias_id}")
        assert r.status_code == 200
        assert client.get("/api/tag-aliases/").json() == []

    def test_same_source_target_is_400(self, client):
        r = client.post("/api/tag-aliases/", json={
            "source": "NTR", "target": "NTR",
        })
        assert r.status_code == 400

    def test_suggest_cursor_resumes_and_wildcards_are_literal(
        self, client, session_factory,
    ):
        """Wire contract of ``/suggest``: opaque integer cursor + literal ``%``.

        The frontend assigns ``next_offset`` to a ref and sends it straight
        back (``AliasSuggestModal.vue``), so it must stay an integer that
        round-trips; the old offset walked a non-deterministic ordering and
        overlapped between pages.  ``%高中生`` is a real tag: unescaped it
        matched every row and the response offered the globally hottest tags
        as its "similar" ones.
        """
        with session_factory() as s:
            s.add(Tag(name="%高中生", reference_count=999))
            for i in range(12):
                s.add(Tag(name=f"cursor-{i:02d}", reference_count=1))
            s.commit()

        r = client.get("/api/tag-aliases/suggest", params={"limit": 2})
        assert r.status_code == 200
        first = r.json()
        assert set(first) == {"items", "next_offset"}
        assert isinstance(first["next_offset"], int)
        assert first["next_offset"] > 0
        assert len(first["items"]) == 2
        # No suggestion for %高中生: nothing contains it literally.
        assert "%高中生" not in {i["target"]["name"] for i in first["items"]}

        r = client.get("/api/tag-aliases/suggest", params={
            "limit": 2, "offset": first["next_offset"],
        })
        assert r.status_code == 200
        second = r.json()
        first_ids = {i["target"]["id"] for i in first["items"]}
        second_ids = {i["target"]["id"] for i in second["items"]}
        assert first_ids.isdisjoint(second_ids), "second page repeated a target"

        r = client.get("/api/tag-aliases/suggest", params={"target_tag": "%高中生"})
        assert r.status_code == 200
        assert r.json() == {"items": [], "next_offset": 0}

    def test_delete_missing_maps_to_404(self, client):
        r = client.delete("/api/tag-aliases/999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Task history wire shape (status lowercase contract)
# ---------------------------------------------------------------------------


class TestTaskHistoryShape:
    LOWER_STATUSES = {"pending", "running", "success", "failed", "interrupted"}

    def test_history_parses_json_and_keeps_status_lowercase(
        self, client, session_factory,
    ):
        with session_factory() as s:
            s.add_all([
                TaskHistory(
                    name="下载小说", task_func="novel_fetch", status="success",
                    arguments='{"id": 1}',
                    start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    result={"summary": "下载完成", "new_novels_count": 1,
                            "new_novel_titles": ["t"]},
                ),
                # Legacy row: written before ``task_func`` existed — the field
                # must serialize as null instead of breaking the response.
                TaskHistory(
                    name="rebuild_fts", status="running",
                    start_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ),
                TaskHistory(
                    name="check_epub", status="failed",
                    start_time=datetime(2026, 1, 3, tzinfo=timezone.utc),
                    result={"summary": "boom"},
                ),
            ])
            s.commit()

        r = client.get("/api/tasks/history")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3

        for item in body["items"]:
            assert item["status"] in self.LOWER_STATUSES
            assert item["status"] == item["status"].lower()

        success = next(i for i in body["items"] if i["task_func"] == "novel_fetch")
        # ``name`` stays the display label; ``task_func`` is the registered
        # function the frontend guard keys on.
        assert success["name"] == "下载小说"
        # JSON columns must arrive as parsed objects, not strings.
        assert success["arguments"] == {"id": 1}
        assert success["result"]["new_novels_count"] == 1
        assert success["result"]["new_novel_titles"] == ["t"]

        legacy_running = next(i for i in body["items"] if i["name"] == "rebuild_fts")
        assert legacy_running["task_func"] is None
