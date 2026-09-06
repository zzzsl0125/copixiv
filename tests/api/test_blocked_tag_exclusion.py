"""Blocked-tag exclusion tests — list / count / ids / match-ids + settings.

Covers the "厌恶标签排除" feature: novels carrying user-blocked tags are
hidden from list-browsing surfaces by default (global setting, default on),
with per-request override via ``exclude_blocked`` and a ``excluded`` count
field on ``/api/novels/count``.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError
from copixiv.db.models import (
    Author, Novel,
)
from copixiv.features.novels import api as novels
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
        pass


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
    app.include_router(
        tag_preferences.router, prefix="/api/tag-preferences",
        tags=["tag_preferences"],
    )
    app.include_router(system.router, prefix="/api/system", tags=["system"])

    with TestClient(app) as c:
        yield c


def _seed_novel(sf, novel_id: int, title: str, path: str, tags=(), **extra):
    """Seed a novel with a tag array (the trigger keeps reference_count in sync)."""
    with sf() as s:
        s.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
        s.flush()
        s.add(Novel(
            id=novel_id, title=title, author_id=novel_id,
            author_name=f"作者{novel_id}", path=path, has_epub=0,
            tags=list(tags), **extra,
        ))
        s.commit()


def _block(client, tag: str):
    r = client.post("/api/tag-preferences/", json={
        "tag": tag, "preference": "blocked",
    })
    assert r.status_code == 200


def _seed_three(session_factory, tmp_path):
    """Novel 1 = NTR (blocked), 2 = 纯爱, 3 = no tags."""
    _seed_novel(session_factory, 1, "标题一", str(tmp_path / "1.txt"),
                tags=["NTR"], like=10)
    _seed_novel(session_factory, 2, "标题二", str(tmp_path / "2.txt"),
                tags=["纯爱"], like=20)
    _seed_novel(session_factory, 3, "标题三", str(tmp_path / "3.txt"),
                like=30)


class TestListExclusion:
    def test_hides_blocked_by_default(self, client, session_factory, tmp_path):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "order_by": "id", "per_page": 20,
        })
        assert r.status_code == 200
        assert [n["id"] for n in r.json()["novels"]] == [3, 2]

    def test_exclude_blocked_false_shows_all(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "order_by": "id", "per_page": 20, "exclude_blocked": "false",
        })
        assert r.status_code == 200
        assert [n["id"] for n in r.json()["novels"]] == [3, 2, 1]
        # Blocked-tag styling data still delivered (red strikethrough).
        assert "NTR" in r.json()["novels"][2]["tags"]

    def test_global_setting_off_shows_all(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        assert client.put("/api/system/config", json={
            "exclude_blocked_tag_novels": False,
        }).status_code == 200

        r = client.get("/api/novels/", params={
            "order_by": "id", "per_page": 20,
        })
        assert [n["id"] for n in r.json()["novels"]] == [3, 2, 1]

    def test_no_blocked_tags_zero_effect(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        r = client.get("/api/novels/", params={
            "order_by": "id", "per_page": 20,
        })
        assert [n["id"] for n in r.json()["novels"]] == [3, 2, 1]

    def test_random_browse_excludes(
        self, client, session_factory, tmp_path,
    ):
        """The shuffle path also excludes — per_page covers everything,
        so both the tail attempt and the wrap-around must skip #1."""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        r = client.get("/api/novels/", params={
            "order_by": "random", "per_page": 50,
        })
        assert r.status_code == 200
        ids = [n["id"] for n in r.json()["novels"]]
        assert 1 not in ids
        assert set(ids) == {2, 3}

    def test_has_excluded_true_when_scoped(
        self, client, session_factory, tmp_path,
    ):
        """首屏搜索范围内有被排除小说 → has_excluded=true。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "keyword": "tags:NTR", "order_by": "id", "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is True
        # 范围内唯一的小说被排除，可见列表为空
        assert body["novels"] == []

    def test_has_excluded_false_when_none_scoped(
        self, client, session_factory, tmp_path,
    ):
        """关键词搜索范围内没有被排除小说（tags:纯爱 只命中 novel 2）→ false。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "keyword": "tags:纯爱", "order_by": "id", "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is False
        assert [n["id"] for n in body["novels"]] == [2]

    def test_has_excluded_false_on_browse(
        self, client, session_factory, tmp_path,
    ):
        """无关键词浏览（首屏）→ has_excluded 保持默认 false。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "order_by": "id", "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is False
        assert [n["id"] for n in body["novels"]] == [3, 2]

    def test_has_excluded_false_on_load_more(
        self, client, session_factory, tmp_path,
    ):
        """带 cursor（load-more 形态）→ 不计算，has_excluded 保持 false。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "keyword": "tags:NTR", "order_by": "id", "per_page": 1,
            "cursor": '{"id": 3}',
        })
        assert r.status_code == 200
        assert r.json()["has_excluded"] is False

    def test_has_excluded_false_when_exclusion_off(
        self, client, session_factory, tmp_path,
    ):
        """exclude_blocked=false（本次不排除）→ has_excluded 保持 false。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/", params={
            "keyword": "tags:NTR", "order_by": "id", "per_page": 20,
            "exclude_blocked": "false",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is False
        # 不排除 → 可见列表包含 novel 1
        assert [n["id"] for n in body["novels"]] == [1]

    def test_has_excluded_favourite_scope(
        self, client, session_factory, tmp_path,
    ):
        """is_favourite 过滤范围：favourite 集内确有被排除小说时才 true。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        # favourite 落在可见的 novel 2 → 范围内无被排除小说
        with session_factory() as s:
            s.get(Novel, 2).is_favourite = True
            s.commit()

        r = client.get("/api/novels/", params={
            "keyword": "is_favourite:true;", "order_by": "id", "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is False
        assert [n["id"] for n in body["novels"]] == [2]

        # favourite 转移到被排除的 novel 1 → 范围内有被排除小说
        with session_factory() as s:
            s.get(Novel, 2).is_favourite = False
            s.get(Novel, 1).is_favourite = True
            s.commit()

        r = client.get("/api/novels/", params={
            "keyword": "is_favourite:true;", "order_by": "id", "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is True
        assert body["novels"] == []

    def test_has_excluded_special_follow_scope(
        self, client, session_factory, tmp_path,
    ):
        """is_special_follow 过滤范围（author 子查询）：同理仅在确有被排除时 true。"""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        # special-follow 落在可见 novel 2 的作者 → 范围内无被排除小说
        with session_factory() as s:
            s.get(Author, 2).is_special_follow = True
            s.commit()

        r = client.get("/api/novels/", params={
            "keyword": "is_special_follow:true;", "order_by": "id",
            "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is False
        assert [n["id"] for n in body["novels"]] == [2]

        # special-follow 转移到被排除 novel 1 的作者 → 范围内有被排除小说
        with session_factory() as s:
            s.get(Author, 2).is_special_follow = False
            s.get(Author, 1).is_special_follow = True
            s.commit()

        r = client.get("/api/novels/", params={
            "keyword": "is_special_follow:true;", "order_by": "id",
            "per_page": 20,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["has_excluded"] is True
        assert body["novels"] == []


class TestCountExclusion:
    def test_count_reports_total_and_excluded(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        # ``with_excluded`` opts into the (expensive) excluded count; the
        # default is excluded=0 because the ExclusionBar is now a lazy
        # "查看被隐藏的小说" action backed by /blocked-ids.
        r = client.get("/api/novels/count")
        assert r.json() == {"total": 2, "excluded": 0}
        r = client.get("/api/novels/count", params={"with_excluded": "true"})
        assert r.json() == {"total": 2, "excluded": 1}

    def test_count_with_filters_excluded_scoped(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        # min_like=20 filters out the blocked novel anyway → excluded 0.
        r = client.get("/api/novels/count", params={"min_like": 20, "with_excluded": "true"})
        assert r.json() == {"total": 2, "excluded": 0}
        # min_like=5 keeps the blocked novel in scope (like=10) → excluded 1.
        r = client.get("/api/novels/count", params={"min_like": 5, "with_excluded": "true"})
        assert r.json() == {"total": 2, "excluded": 1}

    def test_count_override_false(self, client, session_factory, tmp_path):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        r = client.get("/api/novels/count", params={
            "exclude_blocked": "false",
        })
        assert r.json() == {"total": 3, "excluded": 0}


class TestIdsExclusion:
    def test_ids_and_match_ids_exclude(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/ids")
        assert r.status_code == 200
        body = r.json()
        assert body["ids"] == [2, 3]
        assert body["total"] == 2

        # match-ids: selection minus blocked, intersected with scope.
        r = client.post("/api/novels/match-ids", json={
            "novel_ids": [1, 2, 3],
            "keyword": "",
        })
        assert r.status_code == 200
        assert r.json()["matching_ids"] == [2, 3]

    def test_ids_override_false(self, client, session_factory, tmp_path):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        r = client.get("/api/novels/ids", params={
            "exclude_blocked": "false",
        })
        assert r.json()["ids"] == [1, 2, 3]


class TestBlockedIdsEndpoint:
    def test_returns_blocked_ids_in_scope(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/blocked-ids")
        assert r.status_code == 200
        assert r.json() == {"ids": [1], "total": 1, "truncated": False}

    def test_scope_filters_apply(self, client, session_factory, tmp_path):
        """min_like above the blocked novel's like → out of scope."""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        r = client.get("/api/novels/blocked-ids", params={"min_like": 15})
        assert r.json() == {"ids": [], "total": 0, "truncated": False}

        r = client.get("/api/novels/blocked-ids", params={"min_like": 5})
        assert r.json()["ids"] == [1]

    def test_empty_when_exclusion_off(
        self, client, session_factory, tmp_path,
    ):
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        assert client.put("/api/system/config", json={
            "exclude_blocked_tag_novels": False,
        }).status_code == 200

        r = client.get("/api/novels/blocked-ids")
        assert r.json() == {"ids": [], "total": 0, "truncated": False}

    def test_keyword_scope(self, client, session_factory, tmp_path):
        """Keyword search scopes the blocked set (tag filter passes)."""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")

        # novel 2 carries 纯爱 — keyword tag filter intersects blocked set.
        r = client.get("/api/novels/blocked-ids", params={"keyword": "tags:纯爱"})
        assert r.json()["ids"] == []  # blocked novel #1 has no 纯爱

        r = client.get("/api/novels/blocked-ids", params={"keyword": "tags:NTR"})
        assert r.json()["ids"] == [1]

    def test_order_params(self, client, session_factory, tmp_path):
        """order_by=like sorts the blocked set (novel 3 has like=30)."""
        _seed_three(session_factory, tmp_path)
        _block(client, "NTR")
        _block(client, "纯爱")  # novels 1 and 2 both blocked now

        r = client.get("/api/novels/blocked-ids", params={
            "order_by": "like", "order_direction": "DESC",
        })
        assert r.json()["ids"] == [2, 1]  # like 20 > 10

        r = client.get("/api/novels/blocked-ids", params={
            "order_by": "like", "order_direction": "ASC",
        })
        assert r.json()["ids"] == [1, 2]

        # random → scope order (unchanged)
        r = client.get("/api/novels/blocked-ids", params={"order_by": "random"})
        assert set(r.json()["ids"]) == {1, 2}


class TestSortIdsEndpoint:
    def test_sorts_by_like_and_id(self, client, session_factory, tmp_path):
        _seed_three(session_factory, tmp_path)

        r = client.post("/api/novels/sort-ids", json={
            "novel_ids": [1, 2, 3], "order_by": "like", "order_direction": "DESC",
        })
        assert r.status_code == 200
        assert r.json() == {"ids": [3, 2, 1], "total": 3, "truncated": False}

        r = client.post("/api/novels/sort-ids", json={
            "novel_ids": [1, 2, 3], "order_by": "id", "order_direction": "ASC",
        })
        assert r.json()["ids"] == [1, 2, 3]

    def test_drops_missing_and_dedupes(self, client, session_factory, tmp_path):
        _seed_three(session_factory, tmp_path)

        r = client.post("/api/novels/sort-ids", json={
            "novel_ids": [1, 1, 99999, 2], "order_by": "id",
            "order_direction": "ASC",
        })
        assert r.status_code == 200
        assert r.json()["ids"] == [1, 2]
