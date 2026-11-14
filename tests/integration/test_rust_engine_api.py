"""HTTP API integration tests exercising the full FastAPI + Rust query engine stack."""

from __future__ import annotations

from fastapi.testclient import TestClient

from copixiv.app import create_app
from copixiv.config import AppConfig
from copixiv.core.draft import NovelDraft
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


def test_http_endpoints_rust_engine_and_live_mutations(committed_session_factory):
    from copixiv.db import models

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).upsert_novels([
            NovelDraft(id=1, title="One", path="i/1.txt", author_id=10, author_name="Auth10", like=100, text=1000, tags=["tagA"]),
            NovelDraft(id=2, title="Two", path="i/2.txt", author_id=10, author_name="Auth10", like=200, text=2000, tags=["tagA", "spoiler"]),
            NovelDraft(id=-5, title="LocalFive", path="i/neg5.txt", author_id=10, author_name="Auth10", like=300, text=3000, tags=["tagA"]),
            NovelDraft(id=4, title="Four", path="i/4.txt", author_id=20, author_name="Auth20", like=50, text=500, tags=["tagB"]),
        ])
        local_row = uow.session.get(models.Novel, -5)
        assert local_row is not None
        local_row.source = "local"

    cfg = AppConfig(query_engine="rust")
    app = create_app(
        cfg,
        session_factory=committed_session_factory,
        skip_init_db=True,
        skip_auto_backup=True,
        mount_spa=False,
        run_background_tasks=False,
    )

    with TestClient(app, base_url="http://localhost") as client:
        assert app.state.query_engine is not None
        assert app.state.query_engine.ready()

        # Paginated listing & count
        r = client.get("/api/novels/?order_by=like&order_direction=DESC&per_page=2")
        assert r.status_code == 200
        body = r.json()
        assert [n["id"] for n in body["novels"]] == [-5, 2]
        assert body["cursor"] is not None

        r_p2 = client.get(
            "/api/novels/",
            params={
                "order_by": "like",
                "order_direction": "DESC",
                "per_page": 2,
                "cursor": f'{{"id": {body["cursor"]["id"]}, "like": {body["cursor"]["like"]}}}',
            },
        )
        assert r_p2.status_code == 200
        assert [n["id"] for n in r_p2.json()["novels"]] == [1, 4]

        # Count, ids, sort-ids, match-ids
        assert client.get("/api/novels/count").json()["total"] == 4
        assert client.get("/api/novels/ids").json()["ids"] == [-5, 1, 2, 4]
        assert client.post(
            "/api/novels/sort-ids",
            json={"novel_ids": [4, 1, -5], "order_by": "id", "order_direction": "DESC"},
        ).json()["ids"] == [-5, 4, 1]
        assert client.post(
            "/api/novels/match-ids",
            json={"novel_ids": [1, 2, 4], "keyword": "One"},
        ).json()["matching_ids"] == [1]

        # Random pagination across full corpus yields every ID once with string hex cursors
        import json as _json

        rand_ids: list[int] = []
        cursor_obj = None
        for _ in range(5):
            params = {"order_by": "random", "per_page": 2}
            if cursor_obj is not None:
                params["cursor"] = _json.dumps(cursor_obj)
            rr = client.get("/api/novels/", params=params)
            assert rr.status_code == 200
            rb = rr.json()
            rand_ids.extend(n["id"] for n in rb["novels"])
            cursor_obj = rb["cursor"]
            if cursor_obj is None:
                break
            assert isinstance(cursor_obj["seed"], str)
            assert isinstance(cursor_obj["h"], str)
        assert set(rand_ids) == {-5, 1, 2, 4}
        assert len(rand_ids) == 4

        # Block tag via HTTP API -> immediate exclusion in /count, /blocked-ids, has_excluded
        rp = client.post(
            "/api/tag-preferences/",
            json={"tag": "spoiler", "preference": "blocked", "sort_index": 0},
        )
        assert rp.status_code in (200, 201)
        assert client.get("/api/novels/blocked-ids").json()["ids"] == [2]
        rc = client.get("/api/novels/count", params={"with_excluded": True}).json()
        assert rc["total"] == 3
        assert rc["excluded"] == 1
        r_search = client.get("/api/novels/", params={"keyword": "spoiler"}).json()
        assert r_search["novels"] == []
        assert r_search["has_excluded"] is True

        # Toggle system exclude_blocked_tag_novels=false -> all 4 visible immediately
        r_sys = client.put("/api/system/config", json={"exclude_blocked_tag_novels": False})
        assert r_sys.status_code == 200
        rc2 = client.get("/api/novels/count", params={"with_excluded": True}).json()
        assert rc2["total"] == 4
        assert rc2["excluded"] == 0

        # Toggle favourite + special follow -> filtered search reflects immediately
        assert client.post("/api/novels/1/favourite").status_code == 204
        assert client.post("/api/novels/author/10/follow").status_code == 204
        rfav = client.get("/api/novels/ids", params={"keyword": "is_favourite:1"}).json()
        assert rfav["ids"] == [1]
        rsf = client.get("/api/novels/ids", params={"keyword": "is_special_follow:1"}).json()
        assert rsf["ids"] == [-5, 1, 2]

        # Direct Rust-hydrated /by-ids card projection preserves order, reflects flags,
        # and excludes removed `caption` field across both listing and PATCH contracts.
        by_ids = client.post("/api/novels/by-ids", json={"novel_ids": [4, 1, -5, 999999]})
        assert by_ids.status_code == 200
        by_ids_novels = by_ids.json()["novels"]
        assert [n["id"] for n in by_ids_novels] == [4, 1, -5]
        assert by_ids_novels[1]["is_favourite"] == 1
        assert by_ids_novels[1]["is_special_follow"] == 1
        assert all("caption" not in n for n in by_ids_novels)
        assert all("caption" not in n for n in body["novels"])

        bad_caption = client.patch("/api/novels/-5", json={"caption": "nope"})
        assert bad_caption.status_code == 422
