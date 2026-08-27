"""Batch operation endpoint tests — /api/novels/batch (delete / tags).

Covers scope resolution (ids + all_matched with exclusions), the hard
size cap, tag reference-count and FTS maintenance, and file cleanup.
The client fixture mirrors tests/api/test_endpoints_smoke.py.
"""

import zipfile
import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError
from copixiv.db.models import (
    Author, Novel, Tag, NovelTag,
)
from copixiv.features.novels.fts import FTSManager
from copixiv.features.novels import api as novels


class _FakeFileStorage:
    def __init__(self, download_dir: Path):
        self.download_dir = str(download_dir)

    def delete_novel_files(self, novel_path: str) -> None:
        p = Path(novel_path)
        p.unlink(missing_ok=True)
        p.with_suffix(".epub").unlink(missing_ok=True)


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

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])

    with TestClient(app) as c:
        yield c


def _seed(sf, novel_id: int, title: str, path: str, **extra):
    with sf() as s:
        s.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
        s.flush()
        s.add(Novel(
            id=novel_id, title=title, author_id=novel_id,
            author_name=f"作者{novel_id}", path=path,
            has_epub=0, **extra,
        ))
        s.commit()


def _seed_tags(sf, novel_id: int, tags: list[str]):
    with sf() as s:
        for tag in tags:
            t = s.query(Tag).filter_by(name=tag).one_or_none()
            if t is None:
                t = Tag(name=tag, reference_count=0)
                s.add(t)
                s.flush()
            t.reference_count += 1
            s.add(NovelTag(novel_id=novel_id, tag_id=t.id))
        s.commit()


# ---------------------------------------------------------------------------
# POST /api/novels/batch — delete
# ---------------------------------------------------------------------------


class TestBatchDelete:
    def test_ids_mode_removes_rows_files_and_tag_counts(
        self, client, session_factory, tmp_path,
    ):
        for i in (1, 2, 3):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed(session_factory, i, f"标题{i}", str(p))
        _seed_tags(session_factory, 1, ["R-18"])
        _seed_tags(session_factory, 2, ["R-18"])
        _seed_tags(session_factory, 3, ["其他"])

        r = client.post("/api/novels/batch", json={
            "operation": "delete",
            "scope": {"mode": "ids", "novel_ids": [1, 2]},
        })
        assert r.status_code == 200
        assert r.json() == {"matched": 2, "affected": 2}

        assert not (tmp_path / "1.txt").exists()
        assert not (tmp_path / "2.txt").exists()
        assert (tmp_path / "3.txt").exists(), "unselected novel must stay"

        with session_factory() as s:
            assert s.get(Novel, 1) is None
            assert s.get(Novel, 2) is None
            assert s.get(Novel, 3) is not None
            r18 = s.query(Tag).filter_by(name="R-18").one()
            assert r18.reference_count == 0
            other = s.query(Tag).filter_by(name="其他").one()
            assert other.reference_count == 1

    def test_all_matched_with_exclusions(
        self, client, session_factory, tmp_path,
    ):
        for i in range(1, 5):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed(session_factory, i, f"标题{i}", str(p), like=10)

        r = client.post("/api/novels/batch", json={
            "operation": "delete",
            "scope": {
                "mode": "all_matched",
                "keyword": "like:10",
                "excluded_ids": [3],
            },
        })
        assert r.status_code == 200
        assert r.json() == {"matched": 3, "affected": 3}

        with session_factory() as s:
            ids = {
                nid for (nid,) in s.query(Novel.id).all()
            }
            assert ids == {3}, "only the excluded novel must survive"

    def test_empty_ids_scope_is_400(self, client):
        r = client.post("/api/novels/batch", json={
            "operation": "delete",
            "scope": {"mode": "ids", "novel_ids": []},
        })
        assert r.status_code == 400
        assert "勾选" in r.json()["detail"]

    def test_empty_all_matched_scope_is_404(self, client, session_factory):
        _seed(session_factory, 1, "标题", str(Path("/tmp/1.txt")))
        r = client.post("/api/novels/batch", json={
            "operation": "delete",
            "scope": {"mode": "all_matched", "min_like": 999999},
        })
        assert r.status_code == 404

    def test_scope_over_cap_is_400(
        self, client, session_factory, monkeypatch, tmp_path,
    ):
        from copixiv.features.novels import batch_operations
        monkeypatch.setattr(batch_operations, "BATCH_MAX_NOVELS", 2)
        for i in range(1, 4):
            _seed(session_factory, i, f"标题{i}", str(tmp_path / f"{i}.txt"))

        r = client.post("/api/novels/batch", json={
            "operation": "delete",
            "scope": {"mode": "all_matched"},
        })
        assert r.status_code == 400
        assert "上限" in r.json()["detail"]

        r = client.post("/api/novels/batch", json={
            "operation": "delete",
            "scope": {"mode": "ids", "novel_ids": [1, 2, 3]},
        })
        assert r.status_code == 400
        assert "上限" in r.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/novels/batch — tags
# ---------------------------------------------------------------------------


class TestBatchTags:
    def test_add_tags_merges_and_counts(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))
        _seed(session_factory, 2, "标题2", str(Path("/tmp/2.txt")))
        _seed_tags(session_factory, 1, ["R-18"])

        r = client.post("/api/novels/batch", json={
            "operation": "add_tags",
            "scope": {"mode": "ids", "novel_ids": [1, 2]},
            "tags": ["R-18", "新标签"],
        })
        assert r.status_code == 200
        assert r.json() == {"matched": 2, "affected": 2}

        with session_factory() as s:
            r18 = s.query(Tag).filter_by(name="R-18").one()
            assert r18.reference_count == 2
            new_tag = s.query(Tag).filter_by(name="新标签").one()
            assert new_tag.reference_count == 2
            links = sorted(
                s.query(NovelTag.novel_id, NovelTag.tag_id).all()
            )
            r18_id, new_id = r18.id, new_tag.id
            assert links == sorted([
                (1, r18_id), (1, new_id), (2, r18_id), (2, new_id),
            ])

    def test_remove_tags(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))
        _seed(session_factory, 2, "标题2", str(Path("/tmp/2.txt")))
        _seed_tags(session_factory, 1, ["R-18", "保留"])
        _seed_tags(session_factory, 2, ["R-18"])

        r = client.post("/api/novels/batch", json={
            "operation": "remove_tags",
            "scope": {"mode": "ids", "novel_ids": [1, 2]},
            "tags": ["R-18"],
        })
        assert r.status_code == 200
        assert r.json() == {"matched": 2, "affected": 2}

        with session_factory() as s:
            r18 = s.query(Tag).filter_by(name="R-18").one()
            assert r18.reference_count == 0
            kept = s.query(Tag).filter_by(name="保留").one()
            assert kept.reference_count == 1
            assert s.query(NovelTag).filter_by(tag_id=r18.id).count() == 0

    def test_remove_tag_nobody_has_affects_zero(
        self, client, session_factory,
    ):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))

        r = client.post("/api/novels/batch", json={
            "operation": "remove_tags",
            "scope": {"mode": "ids", "novel_ids": [1]},
            "tags": ["不存在的标签"],
        })
        assert r.status_code == 200
        assert r.json() == {"matched": 1, "affected": 0}

    def test_add_tags_empty_is_400(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))
        r = client.post("/api/novels/batch", json={
            "operation": "add_tags",
            "scope": {"mode": "ids", "novel_ids": [1]},
            "tags": ["  "],
        })
        assert r.status_code == 400
        assert "标签" in r.json()["detail"]

    def test_add_tags_updates_fts_index(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))
        with session_factory() as s:
            FTSManager(s).rebuild_novel_fts()

        client.post("/api/novels/batch", json={
            "operation": "add_tags",
            "scope": {"mode": "ids", "novel_ids": [1]},
            "tags": ["幻想"],
        })
        with session_factory() as s:
            row = s.execute(
                __import__("sqlalchemy").text(
                    "SELECT tags FROM novel_fts WHERE rowid = 1"
                )
            ).scalar()
        assert row is not None and "幻想" in row

    def test_unknown_operation_is_400(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))
        r = client.post("/api/novels/batch", json={
            "operation": "explode",
            "scope": {"mode": "ids", "novel_ids": [1]},
        })
        assert r.status_code == 422  # Literal validation rejects it


# ---------------------------------------------------------------------------
# Batch download with selection scope (novel_ids / excluded_ids)
# ---------------------------------------------------------------------------


class TestBatchDownloadSelection:
    def test_novel_ids_restricts_zip(self, client, session_factory, tmp_path):
        for i in (1, 2, 3):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed(session_factory, i, f"标题{i}", str(p))

        r = client.post("/api/novels/batch-download", json={
            "format_mode": "txt", "limit": 10,
            "novel_ids": [1, 3],
        })
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(zf.namelist())
        assert len(names) == 2
        assert all("标题2" not in n for n in names)

    def test_excluded_ids_skipped_in_zip(
        self, client, session_factory, tmp_path,
    ):
        for i in (1, 2, 3):
            p = tmp_path / f"{i}.txt"
            p.write_text(f"正文{i}", encoding="utf-8")
            _seed(session_factory, i, f"标题{i}", str(p))

        r = client.post("/api/novels/batch-download", json={
            "order_by": "id", "order_direction": "ASC",
            "format_mode": "txt", "limit": 10,
            "excluded_ids": [2],
        })
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(zf.namelist())
        assert len(names) == 2
        assert all("标题2" not in n for n in names)

    def test_novel_ids_unknown_ids_ignored(
        self, client, session_factory, tmp_path,
    ):
        p = tmp_path / "1.txt"
        p.write_text("正文", encoding="utf-8")
        _seed(session_factory, 1, "标题1", str(p))

        r = client.post("/api/novels/batch-download", json={
            "format_mode": "txt", "limit": 10,
            "novel_ids": [1, 99999],
        })
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert len(zf.namelist()) == 1

    def test_preview_with_novel_ids(self, client, session_factory, tmp_path):
        p = tmp_path / "1.txt"
        p.write_text("正文", encoding="utf-8")
        _seed(session_factory, 1, "标题一", str(p))
        _seed(session_factory, 2, "标题二", str(tmp_path / "2.txt"))

        r = client.post("/api/novels/batch-download/preview", json={
            "naming_template": "{author_name}/{title}_{id}",
            "novel_ids": [1],
        })
        assert r.status_code == 200
        assert r.json() == {"path": "作者1/标题一_1.txt"}


# ---------------------------------------------------------------------------
# GET /api/novels/count — excluded_ids support
# ---------------------------------------------------------------------------


class TestCountExcluded:
    def test_count_excludes_ids(self, client, session_factory):
        for i in (1, 2, 3):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        r = client.get("/api/novels/count", params={
            "excluded_ids": [2, 3],
        })
        assert r.status_code == 200
        assert r.json() == {"total": 1, "excluded": 0}


# ---------------------------------------------------------------------------
# GET /api/novels/ids — 「全选匹配」bulk-add source
# ---------------------------------------------------------------------------


class TestNovelIdsEndpoint:
    def test_returns_matching_ids_with_total(self, client, session_factory):
        for i in range(1, 5):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")),
                  like=10)

        r = client.get("/api/novels/ids", params={"min_like": 5})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"ids", "total", "truncated"}
        assert body["total"] == 4
        assert sorted(body["ids"]) == [1, 2, 3, 4]
        assert body["truncated"] is False

    def test_returns_full_match_set_without_cap(
        self, client, session_factory, monkeypatch,
    ):
        # The sync 5000 cap must NOT leak into 「全选匹配」: selections may
        # be any size (operations beyond the cap run as background tasks).
        from copixiv.features.novels import api as novels_endpoints
        monkeypatch.setattr(novels_endpoints, "BATCH_MAX_NOVELS", 2)
        for i in range(1, 5):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        r = client.get("/api/novels/ids")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 4
        assert sorted(body["ids"]) == [1, 2, 3, 4]
        assert body["truncated"] is False


# ---------------------------------------------------------------------------
# POST /api/novels/by-ids — 「查看已选」view source
# ---------------------------------------------------------------------------


class TestNovelsByIdsEndpoint:
    def test_returns_novels_in_request_order_with_flags(
        self, client, session_factory,
    ):
        for i in (1, 2, 3):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))
        _seed_tags(session_factory, 1, ["R-18"])

        r = client.post("/api/novels/by-ids", json={"novel_ids": [3, 1]})
        assert r.status_code == 200
        body = r.json()
        assert body["truncated"] is False
        assert [n["id"] for n in body["novels"]] == [3, 1]
        novel = body["novels"][1]
        assert novel["tags"] == ["R-18"]
        assert novel["is_favourite"] == 0
        assert novel["has_epub"] == 0

    def test_missing_ids_dropped(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))

        r = client.post("/api/novels/by-ids", json={
            "novel_ids": [1, 99999],
        })
        assert r.status_code == 200
        assert [n["id"] for n in r.json()["novels"]] == [1]

    def test_truncates_at_batch_cap(
        self, client, session_factory, monkeypatch,
    ):
        from copixiv.features.novels import api as novels_endpoints
        monkeypatch.setattr(novels_endpoints, "BATCH_MAX_NOVELS", 2)
        for i in range(1, 4):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        r = client.post("/api/novels/by-ids", json={"novel_ids": [1, 2, 3]})
        assert r.status_code == 200
        body = r.json()
        assert body["truncated"] is True
        assert len(body["novels"]) == 2


# ---------------------------------------------------------------------------
# POST /api/novels/match-ids — scoped 「清除选择」intersection
# ---------------------------------------------------------------------------


class TestMatchIdsEndpoint:
    def test_returns_only_selected_ids_matching_the_scope(
        self, client, session_factory,
    ):
        for i in range(1, 5):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")),
                  like=10 if i % 2 == 0 else 1)

        r = client.post("/api/novels/match-ids", json={
            "novel_ids": [1, 2, 3, 4],
            "min_like": 5,
        })
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"matching_ids", "truncated"}
        assert sorted(body["matching_ids"]) == [2, 4]
        assert body["truncated"] is False

    def test_no_filters_matches_everything(self, client, session_factory):
        for i in range(1, 4):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        r = client.post("/api/novels/match-ids", json={"novel_ids": [1, 3]})
        assert r.status_code == 200
        assert sorted(r.json()["matching_ids"]) == [1, 3]

    def test_unknown_ids_dropped(self, client, session_factory):
        _seed(session_factory, 1, "标题1", str(Path("/tmp/1.txt")))

        r = client.post("/api/novels/match-ids", json={
            "novel_ids": [1, 99999],
        })
        assert r.status_code == 200
        assert r.json()["matching_ids"] == [1]

    def test_empty_input(self, client):
        r = client.post("/api/novels/match-ids", json={"novel_ids": []})
        assert r.status_code == 200
        assert r.json() == {"matching_ids": [], "truncated": False}

    def test_input_larger_than_internal_chunk_is_fully_processed(
        self, client, session_factory, monkeypatch,
    ):
        from copixiv.features.novels import api as novels_endpoints
        monkeypatch.setattr(novels_endpoints, "BATCH_ID_CHUNK_SIZE", 2)
        for i in range(1, 5):
            _seed(session_factory, i, f"标题{i}", str(Path(f"/tmp/{i}.txt")))

        r = client.post("/api/novels/match-ids", json={
            "novel_ids": [1, 2, 3, 4],
        })
        assert r.status_code == 200
        assert sorted(r.json()["matching_ids"]) == [1, 2, 3, 4]
