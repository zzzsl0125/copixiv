"""End-to-end API smoke test on PostgreSQL (phase 2-B2).

Seeds a small deterministic dataset (hand-built rather than the shared
session-scoped ``seeded_db``, so it can never interfere with other tests via
the shared test DB), then drives the FastAPI endpoints end-to-end:

  list (tags/is_favourite) / keyword & tag filter / count / ids / sort-ids /
  by-ids / match-ids / favourite & special-follow toggle / batch op /
  single delete-cascade / search-history / tag-preferences / tokens / config.

The test is fully self-contained: ``clean_db`` truncates before it runs and
all mutations are restored or performed on dedicated rows.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError
from copixiv.db.models import Author, Novel, NovelSearch, Tag
from copixiv.features.novels.fts import build_search_text
from copixiv.features.novels import api as novels
from copixiv.features.novels import history_api as search_history
from copixiv.features.tags import preferences as tag_preferences
from copixiv.features.tags import aliases as tag_aliases
from copixiv.features.accounts import api as tokens
from copixiv.features.system import api as system


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before the test (PG session-scoped DB)."""
    yield


class _FakeFileStorage:
    def __init__(self, download_dir: Path):
        self.download_dir = str(download_dir)

    def delete_novel_files(self, novel_path: str) -> None:
        pass


class _FakeTaskManager:
    def run_task(self, name: str, params: dict | None = None) -> int:
        return 1


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
    app.include_router(search_history.router, prefix="/api/search-history",
                      tags=["search_history"])
    app.include_router(tag_preferences.router, prefix="/api/tag-preferences",
                      tags=["tag_preferences"])
    app.include_router(tag_aliases.router, prefix="/api/tag-aliases",
                      tags=["tag_aliases"])
    app.include_router(tokens.router, prefix="/api/tokens", tags=["tokens"])
    app.include_router(system.router, prefix="/api/system", tags=["system"])

    with TestClient(app) as c:
        yield c


def _seed_dataset(session_factory, extra_ids=()):
    """Insert a deterministic dataset (title/tags/search rows + authors)."""
    with session_factory() as s:
        # A small corpus: some carry R-18, one title contains 催, one is fav.
        rows = [
            (1, "催眠治疗", ["R-18"], 5000, 5000, True),
            (2, "普通小说", ["日常"], 1000, 3000, False),
            (3, "另一本 R 小说", ["R-18", "特定"], 800, 2000, False),
            (4, "无标签", [], 100, 1000, False),
        ]
        for nid, title, tags, like, text, fav in rows:
            s.add(Author(author_id=nid, author_name=f"作者{nid}"))
            s.flush()
            s.add(Novel(
                id=nid, title=title, author_id=nid, author_name=f"作者{nid}",
                path=f"/tmp/{nid}.txt", like=like, text=text,
                tags=list(tags), is_favourite=fav,
            ))
            s.flush()
            s.add(NovelSearch(
                novel_id=nid,
                search_text=build_search_text(title, f"作者{nid}", None, tags),
            ))
        for nid in extra_ids:
            s.add(Author(author_id=nid, author_name=f"作者{nid}"))
            s.flush()
            s.add(Novel(
                id=nid, title=f"临时{nid}", author_id=nid,
                author_name=f"作者{nid}", path=f"/tmp/{nid}.txt",
                tags=["SMOKE_TAG"], is_favourite=False,
            ))
            s.flush()
            s.add(NovelSearch(
                novel_id=nid,
                search_text=build_search_text(f"临时{nid}", f"作者{nid}", None, ["SMOKE_TAG"]),
            ))
        s.commit()


def _all_visible(client):
    """Fetch every novel with exclusion off."""
    r = client.get("/api/novels/", params={"exclude_blocked": "false", "per_page": 200})
    assert r.status_code == 200
    return r.json()["novels"]


def test_api_pg_smoke(client, session_factory):
    _seed_dataset(session_factory, extra_ids=[901, 902, 903])

    # 1. List: ordering returns rows with tags / is_favourite.
    body = _all_visible(client)
    assert len(body) >= 4
    n0 = body[0]
    assert isinstance(n0["tags"], list)
    assert n0["is_favourite"] in (0, 1)
    assert "is_special_follow" in n0

    # 2. Keyword and tag filters hit.
    r = client.get("/api/novels/", params={"keyword": "催", "exclude_blocked": "false"})
    assert r.status_code == 200
    assert any("催眠" in n["title"] for n in r.json()["novels"])
    r = client.get("/api/novels/", params={"keyword": "tags:R-18", "exclude_blocked": "false"})
    assert r.status_code == 200
    assert len(r.json()["novels"]) >= 2
    for n in r.json()["novels"]:
        assert "R-18" in n["tags"]

    # 3. Count: total/excluded present and consistent with ids.
    r = client.get("/api/novels/count", params={"exclude_blocked": "false"})
    assert r.status_code == 200
    count = r.json()
    assert set(count) == {"total", "excluded"}
    assert count["total"] == len(_all_visible(client))

    # 4. ids / sort-ids / match-ids / by-ids consistency.
    ids_resp = client.get("/api/novels/ids", params={"exclude_blocked": "false"})
    assert ids_resp.status_code == 200
    ids = ids_resp.json()["ids"]
    assert len(ids) == count["total"]
    assert ids_resp.json()["total"] == count["total"]

    sample = ids[:3]
    r = client.post("/api/novels/sort-ids", json={
        "novel_ids": sample, "order_by": "like", "order_direction": "DESC",
    })
    assert r.status_code == 200
    assert set(r.json()["ids"]) == set(sample)

    r = client.post("/api/novels/match-ids", json={"novel_ids": sample})
    assert r.status_code == 200
    assert sorted(r.json()["matching_ids"]) == sorted(sample)

    r = client.post("/api/novels/by-ids", json={"novel_ids": sample})
    assert r.status_code == 200
    by_ids = r.json()["novels"]
    assert [n["id"] for n in by_ids] == sample
    assert all("tags" in n for n in by_ids)

    # 5. Favourite / special-follow toggle ×2 → restore original state.
    fav_id = ids[0]
    orig_fav = next(n["is_favourite"] for n in _all_visible(client) if n["id"] == fav_id)
    r = client.post(f"/api/novels/{fav_id}/favourite")
    assert r.status_code == 204
    fav_state = next(n["is_favourite"] for n in _all_visible(client) if n["id"] == fav_id)
    assert fav_state != orig_fav
    client.post(f"/api/novels/{fav_id}/favourite")
    fav_state2 = next(n["is_favourite"] for n in _all_visible(client) if n["id"] == fav_id)
    assert fav_state2 == orig_fav

    auth_id = by_ids[0]["author_id"]
    client.post(f"/api/novels/author/{auth_id}/follow")
    client.post(f"/api/novels/author/{auth_id}/follow")

    # 6. Batch ops on dedicated rows: add_tags / remove_tags / delete.
    r = client.post("/api/novels/batch", json={
        "operation": "add_tags",
        "scope": {"mode": "ids", "novel_ids": [901, 902]},
        "tags": ["SMOKE_ADD"],
    })
    assert r.status_code == 200
    assert r.json() == {"matched": 2, "affected": 2}
    with session_factory() as s:
        for nid in (901, 902):
            assert "SMOKE_ADD" in s.get(Novel, nid).tags

    r = client.post("/api/novels/batch", json={
        "operation": "remove_tags",
        "scope": {"mode": "ids", "novel_ids": [901, 902]},
        "tags": ["SMOKE_ADD"],
    })
    assert r.status_code == 200
    assert r.json()["affected"] == 2
    with session_factory() as s:
        assert "SMOKE_ADD" not in s.get(Novel, 901).tags

    r = client.post("/api/novels/batch", json={
        "operation": "delete",
        "scope": {"mode": "ids", "novel_ids": [901]},
    })
    assert r.status_code == 200
    assert r.json() == {"matched": 1, "affected": 1}
    with session_factory() as s:
        assert s.get(Novel, 901) is None
        assert s.get(NovelSearch, 901) is None  # FK cascade removes the search row
        assert s.query(Tag).filter_by(name="SMOKE_TAG").one().reference_count == 2

    # 7. Single delete cascades cleanly.
    r = client.delete("/api/novels/903")
    assert r.status_code == 204
    with session_factory() as s:
        assert s.get(Novel, 903) is None
        assert s.get(NovelSearch, 903) is None  # FK cascade removes the search row
        assert s.query(Tag).filter_by(name="SMOKE_TAG").one().reference_count == 1

    # 8. Misc endpoints respond normally.
    assert client.get("/api/search-history/").status_code == 200
    assert client.get("/api/tag-preferences/").status_code == 200
    assert client.get("/api/tokens/").status_code == 200
    cfg = client.get("/api/system/config")
    assert cfg.status_code == 200
    assert set(cfg.json()) == {"batch_download_naming", "exclude_blocked_tag_novels"}
