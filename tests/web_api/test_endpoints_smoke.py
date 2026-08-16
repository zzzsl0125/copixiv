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
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from copixiv.app.config import AppConfig
from copixiv.domain.exceptions import DomainError, NotFoundError
from copixiv.infrastructure.database.models import (
    Author, Novel, SearchHistory, Tag, NovelTag, TaskHistory,
)
from copixiv.web_api.endpoints import (
    novels, tasks, search_history, tag_aliases, tag_preferences, system,
)


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
            status_code=exc.status_code, content={"detail": exc.detail},
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
        assert set(body) == {"novels", "cursor"}
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
        for i in range(1, 6):
            _seed_novel(session_factory, i, f"标题{i}", str(tmp_path / f"{i}.txt"))

        first = client.get(
            "/api/novels/", params={"order_by": "id", "per_page": 2},
        ).json()
        assert [n["id"] for n in first["novels"]] == [5, 4]
        # Keyset cursor: {sort_value, id} — with order_by=id both keys
        # collapse to one; the +1 probe row (id=3) is consumed as the cursor.
        assert first["cursor"] == {"id": 3}

        second = client.get(
            "/api/novels/",
            params={
                "order_by": "id", "per_page": 2,
                "cursor": json.dumps(first["cursor"]),
            },
        ).json()
        assert [n["id"] for n in second["novels"]] == [2, 1]
        assert second["cursor"] is None

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
        assert r.json() == {"total": 1}

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
        names = {m["name"] for m in r.json()}
        assert {"novel_fetch", "rebuild_fts", "check_fts"} <= names

    def test_history_empty(self, client):
        r = client.get("/api/tasks/history")
        assert r.status_code == 200
        assert r.json() == {"items": [], "total": 0}

    def test_create_scheduled_reloads_scheduler(self, client, session_factory):
        r = client.post("/api/tasks/scheduled", json={
            "name": "测试", "task": "rebuild_fts", "cron": "0 4 * * 1",
            "is_enabled": False,
        })
        assert r.status_code == 200
        task = r.json()
        assert task["id"] > 0
        assert task["task"] == "rebuild_fts"
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
            s.add(SearchHistory(type="keyword", value="R-18", timestamp="2026-01-01T00:00:00"))
            s.add(SearchHistory(type="author_id", value="123", timestamp="2026-01-02T00:00:00"))
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
        # And NotFoundError itself carries the right status
        assert NotFoundError("x").status_code == 404


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

    def test_batch_download_limit_501_rejected(self, client):
        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "limit": 501, "format_mode": "txt",
        })
        assert r.status_code == 422

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
                path=str(novel_path),
            ))
            s.add(Tag(name="R-18", reference_count=1))
            s.flush()
            s.add(NovelTag(novel_id=1, tag_id=1))
            s.commit()

        r = client.delete("/api/novels/1")
        assert r.status_code == 204

        assert not novel_path.exists(), "txt file must be deleted"
        assert not epub_path.exists(), "epub file must be deleted"

        with session_factory() as s:
            assert s.get(Novel, 1) is None
            assert s.query(NovelTag).filter_by(novel_id=1).count() == 0
            tag = s.get(Tag, 1)
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
            "default_min_like", "default_min_text", "batch_download_naming",
        }


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
                    name="novel_fetch", status="success",
                    arguments='{"id": 1}',
                    start_time="2026-01-01T00:00:00",
                    result='{"summary": "下载完成", "new_novels_count": 1, "new_novel_titles": ["t"]}',
                ),
                TaskHistory(
                    name="rebuild_fts", status="running",
                    start_time="2026-01-02T00:00:00",
                ),
                TaskHistory(
                    name="check_epub", status="failed",
                    start_time="2026-01-03T00:00:00",
                    result='{"summary": "boom"}',
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

        success = next(i for i in body["items"] if i["name"] == "novel_fetch")
        # JSON columns must arrive as parsed objects, not strings.
        assert success["arguments"] == {"id": 1}
        assert success["result"]["new_novels_count"] == 1
        assert success["result"]["new_novel_titles"] == ["t"]
