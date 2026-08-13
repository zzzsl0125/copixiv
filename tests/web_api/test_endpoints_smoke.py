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

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.app.config import AppConfig
from copixiv.domain.exceptions import DomainError, NotFoundError
from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import (
    Base, Author, Novel, SearchHistory,
)
from copixiv.web_api.endpoints import novels, tasks, search_history


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
            raise ValueError("no such scheduled task")


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return create_session_factory(engine)


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
                "queries": '{"true": "is_favourite"}',
            },
        )
        assert [n["id"] for n in r.json()["novels"]] == [1]
        assert r.json()["novels"][0]["is_favourite"] == 1

    def test_count(self, client, session_factory, tmp_path):
        _seed_novel(session_factory, 1, "标题", str(tmp_path / "1.txt"))
        r = client.get("/api/novels/count")
        assert r.status_code == 200
        assert r.json() == {"total": 1}

    def test_invalid_queries_json_is_400(self, client):
        r = client.get("/api/novels/", params={"queries": "not-json{"})
        assert r.status_code == 400
        assert "Invalid queries JSON" in r.json()["detail"]


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
