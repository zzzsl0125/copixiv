"""Integration tests for M4 series folding, chapter drilldown, series actions, and merged export."""

from __future__ import annotations

import asyncio
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from tests.conftest import FakePixivClient
from tests.factories import make_local_novel, make_test_app

from copixiv.core.draft import build_novel
from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV, EpubStatus
from copixiv.core.naming import build_path
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.sources.entity_resolver import EntityResolver
from copixiv.pixiv.models import (
    NovelListResponse,
    PixivNovelInfo,
    PixivNovelRating,
    PixivSeries,
    PixivTag,
    PixivUser,
    WebviewNovel,
)
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.tasks.batch import BatchExportArgs, batch_export
from copixiv.tasks.executor import TaskContext


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), (40, 100, 180)).save(path, format="PNG")


def _seed_series_fixture(committed_session_factory, download_dir: Path) -> dict[str, Any]:
    t1 = datetime(2025, 1, 10, 12, 0, tzinfo=UTC)
    t2 = datetime(2025, 2, 10, 12, 0, tzinfo=UTC)
    t3 = datetime(2025, 3, 10, 12, 0, tzinfo=UTC)

    p_ch1 = Path(
        build_path(
            901,
            "序章",
            str(download_dir),
            source=SOURCE_PIXIV,
            series_id=900,
            series_name="魔法物语",
            series_index=1,
        )
    )
    p_ch2 = Path(
        build_path(
            902,
            "第二章 冒险",
            str(download_dir),
            source=SOURCE_PIXIV,
            series_id=900,
            series_name="魔法物语",
            series_index=2,
        )
    )
    p_ch3 = Path(
        build_path(
            903,
            "番外篇",
            str(download_dir),
            source=SOURCE_PIXIV,
            series_id=900,
            series_name="魔法物语",
            series_index=None,
        )
    )
    p_solo = Path(build_path(100, "独立短篇", str(download_dir), source=SOURCE_PIXIV))
    for p, txt in (
        (p_ch1, "第一章正文。\n[uploadedimage:1]"),
        (p_ch2, "第二章正文内容。"),
        (p_ch3, "番外篇正文。"),
        (p_solo, "独立短篇正文。"),
    ):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")

    # Give chapter 1 a cover and image so series EPUB embeds c0001_1.jpg
    _write_png(p_ch1.parent / "901_c_cover.png")
    _write_png(p_ch1.parent / "901_u_1.png")

    draft_ch1 = build_novel(
        id=901,
        title="序章",
        author_id=77,
        author_name="魔法作者",
        series_id=900,
        series_name="魔法物语",
        series_index=1,
        text=1000,
        like=50,
        view=200,
        create_time=t1,
        tags=["奇幻", "序"],
        download_dir=str(download_dir),
    )
    from dataclasses import replace

    draft_ch1 = replace(draft_ch1, path=str(p_ch1), has_epub=EpubStatus.DONE)
    assert EpubBuilder().create_epub(draft_ch1, allow_missing_images=True).written is True

    draft_ch2 = replace(
        build_novel(
            id=902,
            title="第二章 冒险",
            author_id=77,
            author_name="魔法作者",
            series_id=900,
            series_name="魔法物语",
            series_index=2,
            text=2000,
            like=800,
            view=9000,
            create_time=t2,
            tags=["主线"],
            download_dir=str(download_dir),
        ),
        path=str(p_ch2),
        has_epub=EpubStatus.NO_IMAGES,
    )
    draft_ch3 = replace(
        build_novel(
            id=903,
            title="番外篇",
            author_id=77,
            author_name="魔法作者",
            series_id=900,
            series_name="魔法物语",
            series_index=None,
            text=500,
            like=120,
            view=400,
            create_time=t3,
            tags=["奇幻", "番外"],
            download_dir=str(download_dir),
        ),
        path=str(p_ch3),
        has_epub=EpubStatus.PENDING,
    )
    draft_solo = replace(
        build_novel(
            id=100,
            title="独立短篇",
            author_id=88,
            author_name="短篇作者",
            text=300,
            like=30,
            view=100,
            create_time=t1,
            tags=["日常"],
            download_dir=str(download_dir),
        ),
        path=str(p_solo),
        has_epub=EpubStatus.NO_IMAGES,
    )

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        resolver = EntityResolver(uow.session)
        local_aid = resolver.resolve_author(SOURCE_LOCAL, "local_auth", name="本地作者")
        local_sid = resolver.resolve_series(SOURCE_LOCAL, "local_ser", name="本地系列书", author_id=local_aid)
        p_l1 = Path(
            build_path(
                -11,
                "本地一",
                str(download_dir),
                source=SOURCE_LOCAL,
                series_id=local_sid,
                series_name="本地系列书",
                series_key="local_ser",
                series_index=1,
            )
        )
        p_l2 = Path(
            build_path(
                -12,
                "本地二",
                str(download_dir),
                source=SOURCE_LOCAL,
                series_id=local_sid,
                series_name="本地系列书",
                series_key="local_ser",
                series_index=2,
            )
        )
        p_l1.parent.mkdir(parents=True, exist_ok=True)
        p_l1.write_text("本地第一章", encoding="utf-8")
        p_l2.write_text("本地第二章", encoding="utf-8")

        persist_novels(uow, [draft_ch1, draft_ch2, draft_ch3, draft_solo])
        srow = uow.session.get(models.Series, 900)
        assert srow is not None
        srow.series_name = "魔法物语"
        srow.author_id = 77
        ch3_row = uow.session.get(models.Novel, 903)
        assert ch3_row is not None
        ch3_row.is_favourite = True

        uow.session.add_all(
            [
                make_local_novel(
                    -11,
                    title="本地一",
                    author_id=local_aid,
                    author_name="本地作者",
                    series_id=local_sid,
                    series_name="本地系列书",
                    series_index=1,
                    path=str(p_l1),
                    like=10,
                    view=20,
                    text=100,
                    tags=["本土"],
                ),
                make_local_novel(
                    -12,
                    title="本地二",
                    author_id=local_aid,
                    author_name="本地作者",
                    series_id=local_sid,
                    series_name="本地系列书",
                    series_index=2,
                    path=str(p_l2),
                    like=25,
                    view=50,
                    text=200,
                    tags=["日常"],
                ),
            ]
        )
        uow.session.flush()
        engine_delta.record_novel_upserts(uow.session, [-11, -12])

    return {
        "local_sid": local_sid,
        "local_aid": local_aid,
        "pixiv_dir": p_ch1.parent,
        "ch_paths": [p_ch1, p_ch2, p_ch3],
    }


def test_series_folding_drilldown_id_redirect_and_actions(
    committed_session_factory,
    tmp_path: Path,
):
    download_dir = tmp_path / "download"
    seeded = _seed_series_fixture(committed_session_factory, download_dir)
    local_sid = seeded["local_sid"]

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # 1. Default folded listing + count: 3 logical books (Pixiv series rep=903, solo=100, local rep=-12)
        count_res = client.get("/api/novels/count")
        assert count_res.status_code == 200
        assert count_res.json()["total"] == 3

        list_res = client.get("/api/novels", params={"order_by": "id", "order_direction": "DESC"})
        assert list_res.status_code == 200
        by_id = {n["id"]: n for n in list_res.json()["novels"]}
        assert set(by_id) == {903, 100, -12}

        pixiv_card = by_id[903]
        assert pixiv_card["title"] == "魔法物语"
        assert pixiv_card["is_series"] is True
        assert pixiv_card["chapter_count"] == 3
        assert pixiv_card["like"] == 800  # peak across chapters (50, 800, 120)
        assert pixiv_card["view"] == 9000  # peak
        assert pixiv_card["text"] == 3500  # sum (1000 + 2000 + 500)
        assert pixiv_card["has_epub"] == EpubStatus.DONE
        assert pixiv_card["is_favourite"] is True
        assert pixiv_card["tags"] == ["主线", "奇幻", "序", "番外"]
        assert pixiv_card["external_url"] == "https://www.pixiv.net/novel/series/900"

        solo_card = by_id[100]
        assert solo_card["is_series"] is False
        assert solo_card["chapter_count"] == 1
        assert solo_card["external_url"] == "https://www.pixiv.net/novel/show.php?id=100"

        local_card = by_id[-12]
        assert local_card["is_series"] is True
        assert local_card["chapter_count"] == 2
        assert local_card["like"] == 25
        assert local_card["text"] == 300
        assert local_card["external_url"] is None

        # 2. Explicit series_id:900; drilldown hits PG and returns individual chapters sorted by series_index
        drill = client.get(
            "/api/novels",
            params={"keyword": "series_id:900;", "order_by": "id", "order_direction": "ASC"},
        )
        assert drill.status_code == 200
        drill_items = drill.json()["novels"]
        assert [c["id"] for c in drill_items] == [901, 902, 903]
        assert all(c["is_series"] is False and c["chapter_count"] == 1 for c in drill_items)
        assert drill_items[0]["external_url"] == "https://www.pixiv.net/novel/show.php?id=901"

        # 3. Search non-representative chapter id:901 redirects to folded series card 903
        redirect = client.get("/api/novels", params={"keyword": "id:901;"})
        assert redirect.status_code == 200
        assert [n["id"] for n in redirect.json()["novels"]] == [903]
        assert redirect.json()["novels"][0]["is_series"] is True

        # 4. On-demand series TXT & EPUB3 download (+ single chapter ?format=epub fix)
        dl_txt = client.get("/api/novels/series/900/download", params={"format": "txt"})
        assert dl_txt.status_code == 200
        assert "魔法物语" in dl_txt.text
        assert "序章" in dl_txt.text
        assert "第二章 冒险" in dl_txt.text
        assert "番外篇" in dl_txt.text
        assert dl_txt.text.index("序章") < dl_txt.text.index("第二章 冒险") < dl_txt.text.index("番外篇")

        for fmt in ("epub", "prefer_epub"):
            dl_epub = client.get("/api/novels/series/900/download", params={"format": fmt})
            assert dl_epub.status_code == 200
            with zipfile.ZipFile(pytest.importorskip("io").BytesIO(dl_epub.content)) as zf:
                names = set(zf.namelist())
                assert {"EPUB/ch_0001.xhtml", "EPUB/ch_0002.xhtml", "EPUB/ch_0003.xhtml"} <= names
                assert "EPUB/images/c0001_1.jpg" in names

        single_epub = client.get("/api/novels/901/download", params={"format": "epub"})
        assert single_epub.status_code == 200
        assert single_epub.headers["content-type"] == "application/epub+zip"

        # Verify atomic chapter files on disk were NOT touched
        for p in seeded["ch_paths"]:
            assert p.is_file()

        # 5. Series favourite toggle applies uniformly across all chapters
        assert client.post("/api/novels/series/900/favourite").status_code == 204
        with committed_session_factory() as s:
            favs = {
                int(nid): fav
                for nid, fav in s.execute(
                    select(models.Novel.id, models.Novel.is_favourite).where(models.Novel.series_id == 900)
                ).all()
            }
            assert favs == {901: False, 902: False, 903: False}

        # 6. Series PATCH: tag union diff + field_policy enforcement
        assert client.patch("/api/novels/series/900", json={"title": "Blocked"}).status_code == 400
        patched_pixiv = client.patch("/api/novels/series/900", json={"tags": ["奇幻", "重构新标签"]})
        assert patched_pixiv.status_code == 200
        assert patched_pixiv.json()["tags"] == ["奇幻", "重构新标签"]

        # Local series PATCH allows title & author_id changes across series + all chapters
        patched_local = client.patch(
            f"/api/novels/series/{local_sid}",
            json={"title": "重命名本地合集", "tags": ["本地更新"]},
        )
        assert patched_local.status_code == 200
        assert patched_local.json()["title"] == "重命名本地合集"
        assert patched_local.json()["series_name"] == "重命名本地合集"
        assert patched_local.json()["tags"] == ["本地更新"]

        # 7. Series sync pulls newly added chapter 904 via Pixiv client outside write tx
        fake = FakePixivClient()
        fake.series_responses[900] = NovelListResponse(
            series=PixivSeries(id=900, title="魔法物语"),
            novels=[
                PixivNovelInfo(
                    id=904,
                    title="第四章 尾声",
                    user=PixivUser(id=77, name="魔法作者"),
                    series=PixivSeries(id=900, title="魔法物语"),
                    tags=[PixivTag(name="完结")],
                    text_length=600,
                    total_bookmarks=999,
                    total_view=12000,
                )
            ],
        )
        fake.webview_responses[904] = WebviewNovel(
            id=904,
            title="第四章 尾声",
            user_id=77,
            text="大结局正文",
            series_id=900,
            series_title="魔法物语",
            tags=["完结"],
            rating=PixivNovelRating(bookmark=999, view=12000),
        )
        app.state.client = fake

        synced = client.post("/api/novels/series/900/sync")
        assert synced.status_code == 200
        synced_json = synced.json()
        assert synced_json["id"] == 904
        assert synced_json["chapter_count"] == 4
        assert synced_json["like"] == 999
        assert "完结" in synced_json["tags"]

        # 8. DELETE /api/novels/series/900 removes DB rows + disk files + empty dir
        assert client.delete("/api/novels/series/900").status_code == 204
        with committed_session_factory() as s:
            assert s.execute(select(models.Novel).where(models.Novel.series_id == 900)).scalars().all() == []
            assert s.get(models.Series, 900) is None
        assert not seeded["pixiv_dir"].exists()


def test_batch_expand_series_and_merge_export_zip(
    committed_session_factory,
    tmp_path: Path,
):
    download_dir = tmp_path / "download"
    _seed_series_fixture(committed_session_factory, download_dir)

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Preview with merge_series=True omits nested series folder in ZIP
        prev = client.post(
            "/api/novels/batch-export/preview",
            json={"novel_ids": [903], "format_mode": "prefer_epub", "merge_series": True},
        )
        assert prev.status_code == 200
        assert prev.json()["path"] == "魔法作者/魔法物语_903.epub"

        # Expand series on batch add_tags updates all 3 chapters (even though only rep 903 was passed)
        tag_res = client.post(
            "/api/novels/batch",
            json={
                "operation": "add_tags",
                "scope": {"novel_ids": [903]},
                "tags": ["全卷标签"],
                "expand_series": True,
            },
        )
        assert tag_res.status_code == 200
        assert tag_res.json() == {"matched": 3, "affected": 3}

        # Enqueue & run batch_export with merge_series=True
        ctx = TaskContext(
            uow=SqlUnitOfWork(committed_session_factory),
            file_storage=app.state.file_storage,
            task_id=55,
        )
        export_res = asyncio.run(
            batch_export(
                BatchExportArgs(
                    novel_ids=[903, 100],
                    format_mode="prefer_epub",
                    merge_series=True,
                ),
                ctx,
            )
        )
        assert "共 2 篇" in export_res.summary
        out_zip = download_dir / "batch_export_55.zip"
        assert out_zip.is_file()
        with zipfile.ZipFile(out_zip) as zf:
            names = set(zf.namelist())
            assert "短篇作者/独立短篇_100.txt" in names
            assert "魔法作者/魔法物语_903.epub" in names

        # Expand series delete removes all 3 chapters of series 900 when only rep 903 is selected
        del_res = client.post(
            "/api/novels/batch",
            json={
                "operation": "delete",
                "scope": {"novel_ids": [903]},
                "expand_series": True,
            },
        )
        assert del_res.status_code == 200
        assert del_res.json() == {"matched": 3, "affected": 3}
        assert client.get("/api/novels/count").json()["total"] == 2  # solo 100 + local -12
