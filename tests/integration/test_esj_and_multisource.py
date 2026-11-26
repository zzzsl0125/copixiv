"""Milestone 2 integration tests: ESJZone end-to-end ingest, sync, retry, search, hydration, and isolation."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from tests.conftest import FakePixivClient
from tests.factories import make_local_novel, make_test_app

from copixiv.core.draft import build_novel
from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_ESJ, SOURCE_LOCAL, SOURCE_PIXIV, EpubStatus
from copixiv.core.naming import NovelNamingTemplate
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import BookChapterIngestor, BookIngestOutcome, persist_novels
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.features.novels.use_cases.batch_edit_metadata import batch_edit_metadata
from copixiv.features.novels.use_cases.delete_novel import delete_novel
from copixiv.features.novels.use_cases.discovery import retry_failed_novels
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.sources.entity_resolver import EntityResolver
from copixiv.features.sources.service import SourceService
from copixiv.query_engine import sync as qe_sync
from copixiv.sources import LocalSourcePlugin, PixivSourcePlugin, SourceHttpManager, SourceRegistry
from copixiv.sources.esj import EsjSourcePlugin
from copixiv.storage.epub import state as epub_state
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.batch import BatchExportArgs, batch_export
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.maintenance.series import FixSeriesIndexArgs, fix_series_index
from copixiv.tasks.sources import SourceFetchArgs, source_fetch


def _make_png_bytes(color: tuple[int, int, int] = (15, 120, 220)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(color: tuple[int, int, int] = (200, 90, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="JPEG")
    return buf.getvalue()


def _book_outline_html(
    *,
    book_id: str,
    title: str,
    author_name: str,
    genre: str = "原創",
    cover_path: str = "/uploads/covers/book_100.jpg",
    views: str = "12.5k",
    likes: str = "1,234",
    tag_widgets: Sequence[Sequence[str]] = (
        ("奇幻", "异世界_别名"),
        ("奇幻", "冒險"),
    ),
    chapters: Sequence[tuple[str, str, bool]] = (),
) -> str:
    """Render an ESJZone book detail HTML fixture with widgets and chapter links."""
    tag_sections = "\n".join(
        "<section class='widget widget-tags'>"
        + "".join(f'<a href="/tags/{idx}/">{tag}</a>' for idx, tag in enumerate(group, start=1))
        + "</section>"
        for group in tag_widgets
    )
    chapter_links: list[str] = []
    for href, label, is_ext in chapters:
        target_attr = ' target="_blank"' if is_ext else ""
        chapter_links.append(f'<a href="{href}"{target_attr}><p>{label}</p></a>')
    cover_html = (
        f'<div class="product-gallery"><img src="data:image/gif;base64,R0lGOD" data-src="{cover_path}"></div>'
        if cover_path
        else ""
    )
    return f"""
    <html>
      <body>
        <div class="row">
          {cover_html}
          <div class="book-detail">
            <h2>  {title}  </h2>
            <ul class="list-unstyled">
              <li>類型：{genre}</li>
              <li>作者: <a href="/tags/author/">{author_name}</a></li>
            </ul>
            <div>
              <span id="vtimes">{views}</span>
              <span id="favorite">{likes}</span>
            </div>
          </div>
        </div>
        {tag_sections}
        <div id="chapterList">
          <details open>
            <summary>第一卷</summary>
            {"".join(chapter_links)}
          </details>
        </div>
      </body>
    </html>
    """


def _chapter_html(
    *,
    title: str,
    paragraphs: Sequence[str],
    image_srcs: Sequence[str] = (),
    poster_uid: str = "77",
    poster_name: str = "原創作者甲",
    published_at: str = "2026-08-15 21:05:30",
) -> str:
    """Render an ESJZone forum chapter HTML fixture."""
    p_html = "".join(f"<p>{p}</p>" for p in paragraphs)
    img_html = "".join(f'<p><img src="{src}" /></p>' for src in image_srcs)
    return f"""
    <html>
      <head><title>{title} - ESJ Zone</title></head>
      <body>
        <h2>{title}</h2>
        <div class="single-post-meta">
          <div><a href="/my/profile?uid={poster_uid}">{poster_name}</a></div>
          <div>發佈時間：{published_at}</div>
        </div>
        <div class="forum-content">
          {p_html}
          {img_html}
        </div>
      </body>
    </html>
    """


def _locked_chapter_html() -> str:
    return """
    <html>
      <body>
        <div class="forum-content">
          <p>內文目前施工中，請輸入密碼</p>
          <button id="btn-send-pw">送出</button>
        </div>
      </body>
    </html>
    """


def _make_pixiv_webview(
    novel_id: int,
    *,
    title: str,
    author_id: int = 90001,
    text: str = "Pixiv 抓取正文內容",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=novel_id,
        title=title,
        user_id=author_id,
        rating={"bookmark": 25, "view": 400},
        caption="Pixiv 简介",
        series_id=None,
        series_title=None,
        series_navigation=None,
        cdate="2026-08-01T12:00:00+09:00",
        tags=["中文", "奇幻"],
        text=text,
        images=[],
        illusts=[],
        cover_url=None,
    )


def _make_ingestor_stack(
    session_factory: Any,
    tmp_path: Path,
    *,
    pixiv_client: Any = None,
) -> tuple[
    SourceHttpManager,
    SourceRegistry,
    SourceService,
    FileStorage,
    NovelAssetPipeline,
    BookChapterIngestor,
]:
    http_manager = SourceHttpManager()
    registry = SourceRegistry(
        [
            PixivSourcePlugin(http_manager=http_manager, client=pixiv_client),
            LocalSourcePlugin(),
            EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0),
        ]
    )
    source_service = SourceService(session_factory, registry, http_manager)
    source_service.bootstrap()
    storage = FileStorage(str(tmp_path / "download"))
    pipeline = NovelAssetPipeline(
        http_manager.get_image_downloader(SOURCE_PIXIV),
        epub_builder=EpubBuilder(),
        download_root=storage.download_dir,
        http_manager=http_manager,
        source_registry=registry,
    )
    ingestor = BookChapterIngestor(
        session_factory=session_factory,
        file_storage=storage,
        image_downloader=pipeline,
        source_registry=registry,
        source_service=source_service,
    )
    return http_manager, registry, source_service, storage, pipeline, ingestor


# ---------------------------------------------------------------------------
# 1. BookChapterIngestor.ingest_book end-to-end with respx + EpubBuilder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_book_chapter_ingestor_ingest_book_end_to_end_and_idempotent_refresh(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end ESJ book ingest covers EPUB policy, failures, entities, tags, and force=False refresh."""

    async def _fast_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr("copixiv.storage.image_downloader.asyncio.sleep", _fast_sleep)

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add(models.TagAlias(source="异世界_别名", target="異世界"))

    http_manager, _registry, source_service, storage, pipeline, ingestor = _make_ingestor_stack(
        committed_session_factory,
        tmp_path,
    )
    source_service.update_config(
        uow,
        SOURCE_ESJ,
        enabled=True,
        config={"request_interval_sec": 0.0},
    )

    cover_bytes = _make_jpeg_bytes((180, 60, 20))
    body_png = _make_png_bytes((20, 140, 220))
    outline_v1 = _book_outline_html(
        book_id="100",
        title="異世界冒險錄",
        author_name="原創作者甲",
        genre="原創",
        cover_path="/uploads/covers/book_100.jpg",
        views="12.5k",
        likes="1,234",
        tag_widgets=(("奇幻", "异世界_别名"), ("奇幻", "冒險")),
        chapters=(
            ("https://www.esjzone.cc/forum/100/1001.html", "第一章 全圖成功", False),
            ("/forum/100/1002.html", "第二章 部分圖成功", False),
            ("/forum/100/1003.html", "第三章 全圖失敗", False),
            ("https://ncode.syosetu.com/n9999aa/", "日文生肉外鏈", True),
            ("/forum/100/1004.html", "第四章 密碼鎖定", False),
        ),
    )

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/100.html").mock(
                return_value=httpx.Response(200, text=outline_v1)
            )
            mock.get("https://www.esjzone.cc/forum/100/1001.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(
                        title="第一章 全圖成功",
                        paragraphs=["第一章正文內容"],
                        image_srcs=["/assets/img/p1.png"],
                    ),
                )
            )
            mock.get("https://www.esjzone.cc/forum/100/1002.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(
                        title="第二章 部分圖成功",
                        paragraphs=["第二章正文內容"],
                        image_srcs=["/assets/img/p2_ok.png", "/assets/img/p2_missing.jpg"],
                    ),
                )
            )
            mock.get("https://www.esjzone.cc/forum/100/1003.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(
                        title="第三章 全圖失敗",
                        paragraphs=["第三章正文內容"],
                        image_srcs=["/assets/img/p3_missing.jpg"],
                    ),
                )
            )
            mock.get("https://www.esjzone.cc/forum/100/1004.html").mock(
                return_value=httpx.Response(200, text=_locked_chapter_html())
            )
            mock.get("https://www.esjzone.cc/uploads/covers/book_100.jpg").mock(
                return_value=httpx.Response(200, content=cover_bytes)
            )
            mock.get("https://www.esjzone.cc/assets/img/p1.png").mock(
                return_value=httpx.Response(200, content=body_png)
            )
            mock.get("https://www.esjzone.cc/assets/img/p2_ok.png").mock(
                return_value=httpx.Response(200, content=body_png)
            )
            mock.get("https://www.esjzone.cc/assets/img/p2_missing.jpg").mock(
                return_value=httpx.Response(404, text="Not Found")
            )
            mock.get("https://www.esjzone.cc/assets/img/p3_missing.jpg").mock(
                return_value=httpx.Response(404, text="Not Found")
            )

            outcome = await ingestor.ingest_book(SOURCE_ESJ, "100", force=False)

        assert isinstance(outcome, BookIngestOutcome)
        assert outcome.new_count == 3
        assert outcome.skipped_external == 1
        assert outcome.skipped_existing == 0
        assert outcome.failure_count == 1
        assert len(outcome.failed_keys) == 1
        assert outcome.failed_keys[0][0] == "100/1004"

        with committed_session_factory() as session:
            author = session.execute(
                select(models.Author).where(models.Author.source == SOURCE_ESJ)
            ).scalar_one()
            assert author.author_id < 0
            assert author.source_key == "uid:77"
            assert author.author_name == "原創作者甲"

            series = session.execute(
                select(models.Series).where(models.Series.source == SOURCE_ESJ)
            ).scalar_one()
            assert series.series_id < 0
            assert series.source_key == "100"
            assert series.series_name == "異世界冒險錄"
            assert series.author_id == author.author_id

            novels = {
                n.source_key: n
                for n in session.execute(
                    select(models.Novel).where(models.Novel.source == SOURCE_ESJ)
                ).scalars()
            }
            assert set(novels) == {"100/1001", "100/1002", "100/1003"}

            n1, n2, n3 = novels["100/1001"], novels["100/1002"], novels["100/1003"]
            assert n1.id < 0
            assert n2.id < 0
            assert n3.id < 0
            assert n1.author_id == author.author_id
            assert n1.series_id == series.series_id
            assert (n1.series_index, n2.series_index, n3.series_index) == (1, 2, 3)
            assert n1.like == 1234
            assert n1.view == 12500
            assert n1.tags == ["冒險", "奇幻", "異世界"]
            assert n1.has_epub == EpubStatus.DONE
            assert n2.has_epub == EpubStatus.DONE
            assert n3.has_epub == EpubStatus.NO_IMAGES

            esj_dir = Path(storage.download_dir) / "esj"
            for row in (n1, n2, n3):
                assert row.path is not None
                txt_path = Path(row.path)
                assert txt_path.parent == esj_dir
                assert txt_path.name == f"{row.title}_{row.id}.txt"
                assert txt_path.is_file()

            epub_1 = Path(n1.path).with_suffix(".epub")
            epub_2 = Path(n2.path).with_suffix(".epub")
            epub_3 = Path(n3.path).with_suffix(".epub")
            assert epub_state.inspect_epub(epub_1) == (True, True)
            assert epub_state.inspect_epub(epub_2) == (True, True)
            assert not epub_3.exists()

            with zipfile.ZipFile(epub_2) as zf:
                assert "EPUB/cover.jpg" in zf.namelist()
                assert "EPUB/images/1.jpg" in zf.namelist()
                xhtml = zf.read("EPUB/content.xhtml").decode("utf-8")
                assert "illust-missing" in xhtml
                assert "[uploadedimage:" not in xhtml

            # Temporary downloaded images were cleaned up after EPUB packaging.
            assert not list(esj_dir.glob("*_c_cover.*"))
            assert not list(esj_dir.glob("*_u_*"))

            # All-failed-image chapter 100/1003 is NOT recorded as a failure; only 100/1004 is.
            failures = session.execute(select(models.FailedNovel)).scalars().all()
            assert len(failures) == 1
            f_row = failures[0]
            assert f_row.source == SOURCE_ESJ
            assert f_row.source_key == "100/1004"
            assert f_row.novel_id is None
            assert f_row.extra == {"book_id": "100", "chapter_id": "1004", "series_index": 5}

            # Sentinel body & manual tag to verify force=False preserves body + merges tags.
            n1_path = Path(n1.path)
            n1_id = int(n1.id)
            n2_id = int(n2.id)
            n3_id = int(n3.id)

        sentinel_body = n1_path.read_text(encoding="utf-8") + "\n[本地哨兵文本]"
        n1_path.write_text(sentinel_body, encoding="utf-8")
        with uow.begin():
            row_n1 = uow.session.get(models.Novel, n1_id)
            assert row_n1 is not None
            row_n1.tags = sorted([*row_n1.tags, "手動標籤"])
            engine_delta.record_novel_upserts(uow.session, [n1_id])

        # Re-run ingest_book(..., force=False): existing chapters 1001/1002/1003 are NOT re-fetched.
        outline_v2 = _book_outline_html(
            book_id="100",
            title="異世界冒險錄",
            author_name="原創作者甲",
            genre="原創",
            cover_path="/uploads/covers/book_100.jpg",
            views="99,999",
            likes="8,888",
            tag_widgets=(("奇幻", "熱血"),),
            chapters=(
                ("https://www.esjzone.cc/forum/100/1001.html", "第一章 全圖成功", False),
                ("https://ncode.syosetu.com/n9999aa/", "日文生肉外鏈", True),
                ("/forum/100/1003.html", "第三章 全圖失敗", False),
                ("/forum/100/1002.html", "第二章 部分圖成功", False),
                ("/forum/100/1004.html", "第四章 密碼鎖定", False),
            ),
        )
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/100.html").mock(
                return_value=httpx.Response(200, text=outline_v2)
            )
            # Only 100/1004 (previous failure with failed_times=1 < 3) is fetched; 1001/1002/1003 are NOT mocked.
            mock.get("https://www.esjzone.cc/forum/100/1004.html").mock(
                return_value=httpx.Response(200, text=_locked_chapter_html())
            )
            outcome2 = await ingestor.ingest_book(SOURCE_ESJ, "100", force=False)

        assert outcome2.new_count == 0
        assert outcome2.skipped_existing == 3
        assert outcome2.skipped_external == 1
        assert outcome2.failure_count == 1
        assert n1_path.read_text(encoding="utf-8") == sentinel_body

        with committed_session_factory() as session:
            u1 = session.get(models.Novel, n1_id)
            u2 = session.get(models.Novel, n2_id)
            u3 = session.get(models.Novel, n3_id)
            assert u1 is not None
            assert u2 is not None
            assert u3 is not None
            assert (u1.series_index, u3.series_index, u2.series_index) == (1, 3, 4)
            assert (u1.like, u1.view) == (8888, 99999)
            assert (u2.like, u2.view) == (8888, 99999)
            assert (u3.like, u3.view) == (8888, 99999)
            assert u1.has_epub == EpubStatus.DONE
            assert u2.has_epub == EpubStatus.DONE
            assert u3.has_epub == EpubStatus.NO_IMAGES
            assert set(u1.tags) == {"冒險", "奇幻", "手動標籤", "熱血", "異世界"}
            assert set(u2.tags) == {"冒險", "奇幻", "熱血", "異世界"}
    finally:
        await pipeline.aclose()
        await http_manager.aclose()


# ---------------------------------------------------------------------------
# 2. ESJ POST /api/novels/{id}/sync and retry_failed_novels
# ---------------------------------------------------------------------------


def test_esj_single_chapter_sync_and_retry_failed_novels(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Single-chapter sync updates text/metadata preserving path & has_epub, and retry_failed_novels unlocks/clears."""

    async def _fast_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr("copixiv.storage.image_downloader.asyncio.sleep", _fast_sleep)

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        assert (
            client.put(
                "/api/sources/esj/config",
                json={"enabled": True, "config": {"request_interval_sec": 0.0}},
            ).status_code
            == 200
        )

        ingestor = BookChapterIngestor(
            session_factory=app.state.session_factory,
            file_storage=app.state.file_storage,
            image_downloader=app.state.image_downloader,
            source_registry=app.state.source_registry,
            source_service=app.state.source_service,
        )
        outline_v1 = _book_outline_html(
            book_id="100",
            title="同步測試書",
            author_name="作者甲",
            genre="原創",
            cover_path="",
            views="200",
            likes="20",
            tag_widgets=(("奇幻",),),
            chapters=(
                ("/forum/100/1001.html", "第一章 初版", False),
                ("/forum/100/1004.html", "第二章 密碼章", False),
            ),
        )
        png_bytes = _make_png_bytes()

        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/100.html").mock(
                return_value=httpx.Response(200, text=outline_v1)
            )
            mock.get("https://www.esjzone.cc/forum/100/1001.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(
                        title="第一章 初版",
                        paragraphs=["初版正文"],
                        image_srcs=["/assets/img/c1.png"],
                    ),
                )
            )
            mock.get("https://www.esjzone.cc/forum/100/1004.html").mock(
                return_value=httpx.Response(200, text=_locked_chapter_html())
            )
            mock.get("https://www.esjzone.cc/assets/img/c1.png").mock(
                return_value=httpx.Response(200, content=png_bytes)
            )
            asyncio.run(ingestor.ingest_book(SOURCE_ESJ, "100", force=False))

        with committed_session_factory() as session:
            n1 = session.execute(
                select(models.Novel).where(models.Novel.source_key == "100/1001")
            ).scalar_one()
            n1_id = int(n1.id)
            original_path = str(n1.path)
            assert n1.has_epub == EpubStatus.DONE

        # 1) POST /api/novels/{id}/sync updates text & book metadata while preserving path and has_epub
        outline_sync = _book_outline_html(
            book_id="100",
            title="同步測試書",
            author_name="作者甲",
            genre="原創",
            cover_path="",
            views="5,000",
            likes="777",
            tag_widgets=(("奇幻", "修訂版"),),
            chapters=(
                ("/forum/100/1001.html", "第一章 修訂標題", False),
                ("/forum/100/1004.html", "第二章 密碼章", False),
            ),
        )
        updated_body = "修訂後的全新章節正文，字數更長"
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/100.html").mock(
                return_value=httpx.Response(200, text=outline_sync)
            )
            mock.get("https://www.esjzone.cc/forum/100/1001.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(
                        title="第一章 修訂標題",
                        paragraphs=[updated_body],
                    ),
                )
            )
            sync_res = client.post(f"/api/novels/{n1_id}/sync")

        assert sync_res.status_code == 200, sync_res.text
        sync_body = sync_res.json()
        assert sync_body["title"] == "第一章 修訂標題"
        assert sync_body["like"] == 777
        assert sync_body["view"] == 5000
        assert sync_body["text"] == len(updated_body)
        assert sync_body["has_epub"] == int(EpubStatus.DONE)
        assert set(sync_body["tags"]) == {"奇幻", "修訂版"}
        assert sync_body["external_url"] == "https://www.esjzone.cc/forum/100/1001.html"
        assert sync_body["can_sync"] is True

        with committed_session_factory() as session:
            n1_after = session.get(models.Novel, n1_id)
            assert n1_after is not None
            assert n1_after.path == original_path
            assert n1_after.has_epub == EpubStatus.DONE
            assert Path(original_path).read_text(encoding="utf-8") == updated_body

        # 2) Remote 404 during sync returns HTTP 404
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/100.html").mock(
                return_value=httpx.Response(200, text=outline_sync)
            )
            mock.get("https://www.esjzone.cc/forum/100/1001.html").mock(
                return_value=httpx.Response(404, text="Not Found")
            )
            not_found_res = client.post(f"/api/novels/{n1_id}/sync")
        assert not_found_res.status_code == 404

        # 3) Inspect failed_novel row via GET /api/failed-novels
        failed_list = client.get("/api/failed-novels")
        assert failed_list.status_code == 200
        assert failed_list.json()["total"] == 1
        failed_item = failed_list.json()["items"][0]
        failed_row_id = failed_item["id"]
        assert failed_item["source"] == SOURCE_ESJ
        assert failed_item["source_key"] == "100/1004"
        assert failed_item["novel_id"] is None
        assert failed_item["external_url"] == "https://www.esjzone.cc/forum/100/1004.html"

        # 4) Disabled ESJ source skips retry gracefully without any HTTP call
        assert client.put("/api/sources/esj/config", json={"enabled": False}).status_code == 200
        with respx.mock(assert_all_called=True):
            disabled_retry = asyncio.run(
                retry_failed_novels(
                    ids=[failed_row_id],
                    session_factory=committed_session_factory,
                    client=app.state.client,
                    file_storage=app.state.file_storage,
                    image_downloader=app.state.image_downloader,
                    source_service=app.state.source_service,
                )
            )
        assert "跳过 1 条" in disabled_retry.summary
        assert disabled_retry.failed_ids == []
        assert client.get("/api/failed-novels").json()["total"] == 1

        # 5) Re-enable ESJ + add chapter_passwords, then enqueue and run retry_failed_novels
        assert (
            client.put(
                "/api/sources/esj/config",
                json={
                    "enabled": True,
                    "config": {
                        "request_interval_sec": 0.0,
                        "chapter_passwords": {"100/1004": "unlock_pw"},
                    },
                },
            ).status_code
            == 200
        )

        enqueue_retry = client.post("/api/failed-novels/retry", json={"ids": [failed_row_id]})
        assert enqueue_retry.status_code == 200
        assert enqueue_retry.json()["matched"] == 1

        unlocked_html = _chapter_html(
            title="第二章 解鎖成功",
            paragraphs=["密碼解鎖後的正文內容"],
        )
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/100.html").mock(
                return_value=httpx.Response(200, text=outline_sync)
            )
            ch_route = mock.get("https://www.esjzone.cc/forum/100/1004.html").mock(
                side_effect=[
                    httpx.Response(200, text=_locked_chapter_html()),
                    httpx.Response(200, text=unlocked_html),
                ]
            )
            token_route = mock.post("https://www.esjzone.cc/forum/1004/100.html").mock(
                return_value=httpx.Response(200, text="<JinJing>tok-retry-123</JinJing>")
            )
            pw_route = mock.post("https://www.esjzone.cc/inc/forum_pw.php").mock(
                return_value=httpx.Response(
                    200,
                    text="ok",
                    headers={"Set-Cookie": "esj_pw=ok; Path=/"},
                )
            )

            retry_res = asyncio.run(
                retry_failed_novels(
                    ids=[failed_row_id],
                    session_factory=committed_session_factory,
                    client=app.state.client,
                    file_storage=app.state.file_storage,
                    image_downloader=app.state.image_downloader,
                    source_service=app.state.source_service,
                )
            )

            assert ch_route.call_count == 2
            assert token_route.call_count == 1
            assert pw_route.call_count == 1
            assert b"pw=unlock_pw" in pw_route.calls[0].request.content

        assert "成功 1/1" in retry_res.summary
        assert retry_res.failed_ids == []
        assert retry_res.new_novel_titles == ["第二章 解鎖成功"]

        with committed_session_factory() as session:
            n_unlocked = session.execute(
                select(models.Novel).where(models.Novel.source_key == "100/1004")
            ).scalar_one()
            assert n_unlocked.id < 0
            assert n_unlocked.series_index == 2
            assert n_unlocked.path is not None
            assert Path(n_unlocked.path).read_text(encoding="utf-8") == "密碼解鎖後的正文內容"
            assert session.execute(select(models.FailedNovel)).scalars().all() == []

        assert client.get("/api/failed-novels").json()["total"] == 0


# ---------------------------------------------------------------------------
# 3. POST /api/sources/fetch + source_fetch task
# ---------------------------------------------------------------------------


def test_sources_fetch_api_validation_and_source_fetch_task(
    committed_session_factory,
    tmp_path: Path,
):
    """``POST /api/sources/fetch`` enforces source enablement/ref syntax, and ``source_fetch`` handles ESJ & Pixiv."""
    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # ESJ disabled by default -> 400
        disabled_res = client.post(
            "/api/sources/fetch",
            json={"refs": ["https://www.esjzone.cc/detail/201.html"]},
        )
        assert disabled_res.status_code == 400
        assert "已禁用" in disabled_res.json()["detail"]

        # Invalid or blank refs -> 400 / 422
        assert (
            client.post(
                "/api/sources/fetch",
                json={"refs": ["https://unknown.example.com/book/1"]},
            ).status_code
            == 400
        )
        assert client.post("/api/sources/fetch", json={"refs": ["   "]}).status_code == 400
        assert client.post("/api/sources/fetch", json={"refs": []}).status_code == 422

        # Enable ESJ
        assert (
            client.put(
                "/api/sources/esj/config",
                json={"enabled": True, "config": {"request_interval_sec": 0.0}},
            ).status_code
            == 200
        )

        # Single ESJ book ref -> 200 + TaskHistory row queued
        fetch_book_res = client.post(
            "/api/sources/fetch",
            json={"refs": ["https://www.esjzone.cc/detail/201.html"]},
        )
        assert fetch_book_res.status_code == 200, fetch_book_res.text
        fetch_body = fetch_book_res.json()
        assert fetch_body["source"] == SOURCE_ESJ
        assert fetch_body["ref_count"] == 1
        task_id = fetch_body["task_id"]

        with committed_session_factory() as session:
            task_row = session.get(models.TaskHistory, task_id)
            assert task_row is not None
            assert task_row.task_func == "source_fetch"
            assert task_row.arguments == {
                "source": SOURCE_ESJ,
                "refs": ["https://www.esjzone.cc/detail/201.html"],
                "force": False,
            }

        # Mixed ESJ chapter + Pixiv URL -> 200 with source="mixed"
        mixed_res = client.post(
            "/api/sources/fetch",
            json={
                "refs": [
                    "https://www.esjzone.cc/forum/202/3001.html",
                    "https://www.pixiv.net/novel/show.php?id=9001",
                ]
            },
        )
        assert mixed_res.status_code == 200
        assert mixed_res.json()["source"] == "mixed"
        assert mixed_res.json()["ref_count"] == 2

        # Execute source_fetch task directly for ESJ book URL, ESJ chapter URL, and Pixiv URL
        fake_pixiv = FakePixivClient()
        fake_pixiv.webview_responses[9001] = _make_pixiv_webview(9001, title="Pixiv引用抓取", author_id=90001)
        fake_pixiv.user_detail_responses[90001] = {"user": {"id": 90001, "name": "Pixiv作者90001"}}

        ctx = TaskContext(
            uow=SqlUnitOfWork(committed_session_factory),
            session_factory=committed_session_factory,
            client=fake_pixiv,  # type: ignore[arg-type]
            file_storage=app.state.file_storage,
            image_downloader=app.state.image_downloader,
            source_registry=app.state.source_registry,
            source_service=app.state.source_service,
        )

        outline_201 = _book_outline_html(
            book_id="201",
            title="按引用抓書201",
            author_name="作者201",
            genre="日輕",
            cover_path="",
            chapters=(("/forum/201/2101.html", "書201第一章", False),),
        )
        outline_202 = _book_outline_html(
            book_id="202",
            title="按引用抓章202",
            author_name="作者202",
            genre="原創",
            cover_path="",
            chapters=(
                ("/forum/202/3001.html", "書202目標章", False),
                ("/forum/202/3002.html", "書202未抓取章", False),
            ),
        )

        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/201.html").mock(
                return_value=httpx.Response(200, text=outline_201)
            )
            mock.get("https://www.esjzone.cc/forum/201/2101.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(title="書201第一章", paragraphs=["201正文"]),
                )
            )
            mock.get("https://www.esjzone.cc/detail/202.html").mock(
                return_value=httpx.Response(200, text=outline_202)
            )
            mock.get("https://www.esjzone.cc/forum/202/3001.html").mock(
                return_value=httpx.Response(
                    200,
                    text=_chapter_html(
                        title="書202目標章",
                        paragraphs=["3001正文"],
                        poster_uid="2020",
                        poster_name="作者202",
                    ),
                )
            )

            book_task_res = asyncio.run(
                source_fetch(
                    SourceFetchArgs(refs=["https://www.esjzone.cc/detail/201.html"]),
                    ctx,
                )
            )
            chapter_pixiv_res = asyncio.run(
                source_fetch(
                    SourceFetchArgs(
                        refs=[
                            "https://www.esjzone.cc/forum/202/3001.html",
                            "https://www.pixiv.net/novel/show.php?id=9001",
                        ]
                    ),
                    ctx,
                )
            )

        assert "新增 1 本小说" in book_task_res.summary
        assert book_task_res.new_novel_titles == ["書201第一章"]
        assert "新增 2 本小说" in chapter_pixiv_res.summary
        assert set(chapter_pixiv_res.new_novel_titles) == {"Pixiv引用抓取", "書202目標章"}

        with committed_session_factory() as session:
            esj_201 = session.execute(
                select(models.Novel).where(models.Novel.source_key == "201/2101")
            ).scalar_one()
            esj_202 = session.execute(
                select(models.Novel).where(models.Novel.source_key == "202/3001")
            ).scalar_one()
            esj_202_skipped = session.execute(
                select(models.Novel).where(models.Novel.source_key == "202/3002")
            ).scalar_one_or_none()
            pixiv_9001 = session.get(models.Novel, 9001)

            assert esj_201.id < 0
            assert esj_201.source == SOURCE_ESJ
            assert esj_202.id < 0
            assert esj_202.source == SOURCE_ESJ
            assert esj_202.series_index == 1
            assert esj_202_skipped is None
            assert pixiv_9001 is not None
            assert pixiv_9001.source == SOURCE_PIXIV
            assert Path(esj_201.path).is_file()
            assert Path(esj_202.path).is_file()
            assert Path(pixiv_9001.path).is_file()


# ---------------------------------------------------------------------------
# 4. Cross-source GET /api/novels hydration (external_url & can_sync)
# ---------------------------------------------------------------------------


def test_cross_source_novel_hydration_and_dynamic_esj_base_url(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Switching ESJ ``base_url`` or disabling ESJ updates ``external_url`` & ``can_sync`` without engine rebuild."""
    download_dir = tmp_path / "download"
    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        resolver = EntityResolver(uow.session)
        esj_aid = resolver.resolve_author(SOURCE_ESJ, "uid:500", name="ESJ作者")
        esj_sid = resolver.resolve_series(SOURCE_ESJ, "500", name="ESJ書名", author_id=esj_aid)
        esj_nid = resolver.next_novel_id()

        persist_novels(
            uow,
            [
                build_novel(
                    id=5002,
                    title="Pixiv跨源測試",
                    author_id=50020,
                    author_name="Pixiv作者",
                    has_epub=EpubStatus.NO_IMAGES,
                    download_dir=str(download_dir),
                ),
            ],
        )
        uow.session.add_all(
            [
                models.Novel(
                    id=esj_nid,
                    title="ESJ跨源測試章",
                    author_id=esj_aid,
                    author_name="ESJ作者",
                    series_id=esj_sid,
                    series_name="ESJ書名",
                    series_index=1,
                    path=str(download_dir / "esj" / f"ESJ跨源測試章_{esj_nid}.txt"),
                    like=10,
                    view=100,
                    text=50,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=["奇幻"],
                    source=SOURCE_ESJ,
                    source_key="500/5001",
                ),
                make_local_novel(
                    -200,
                    title="Local跨源測試",
                    author_id=None,
                    author_name="本地作者",
                    path=str(download_dir / "local" / "Local跨源測試_-200.txt"),
                ),
            ]
        )
        uow.session.flush()
        engine_delta.record_novel_upserts(uow.session, [esj_nid, -200])

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.put("/api/sources/esj/config", json={"enabled": True}).status_code == 200

        def _assert_hydration(expected_esj_url: str, *, expected_esj_sync: bool) -> None:
            list_res = client.get("/api/novels", params={"order_by": "id", "per_page": 20})
            assert list_res.status_code == 200
            by_id_list = {item["id"]: item for item in list_res.json()["novels"]}
            assert set(by_id_list) >= {esj_nid, 5002, -200}

            by_ids_res = client.post("/api/novels/by-ids", json={"novel_ids": [esj_nid, 5002, -200]})
            assert by_ids_res.status_code == 200
            by_id_cards = {item["id"]: item for item in by_ids_res.json()["novels"]}

            for mapping in (by_id_list, by_id_cards):
                esj_item = mapping[esj_nid]
                assert esj_item["source"] == SOURCE_ESJ
                assert esj_item["external_url"] == expected_esj_url
                assert esj_item["can_sync"] is expected_esj_sync

                pixiv_item = mapping[5002]
                assert pixiv_item["source"] == SOURCE_PIXIV
                assert pixiv_item["external_url"] == "https://www.pixiv.net/novel/show.php?id=5002"
                assert pixiv_item["can_sync"] is True

                local_item = mapping[-200]
                assert local_item["source"] == SOURCE_LOCAL
                assert local_item["external_url"] is None
                assert local_item["can_sync"] is False

        _assert_hydration("https://www.esjzone.cc/forum/500/5001.html", expected_esj_sync=True)

        # Spy on Rust engine rebuild / delta sync to verify base_url switch needs zero engine rebuild.
        engine_calls: list[str] = []
        engine_instance = app.state.query_engine
        orig_swap = engine_instance.swap_from_staging
        orig_apply = engine_instance.apply_delta

        def _spy_build(*args, **kwargs):
            engine_calls.append("build_engine_from_store")
            return qe_sync.build_engine_from_store(*args, **kwargs)

        def _spy_swap(*args, **kwargs):
            engine_calls.append("swap_from_staging")
            return orig_swap(*args, **kwargs)

        def _spy_apply(*args, **kwargs):
            engine_calls.append("apply_delta")
            return orig_apply(*args, **kwargs)

        monkeypatch.setattr(qe_sync, "build_engine_from_store", _spy_build)
        monkeypatch.setattr(engine_instance, "swap_from_staging", _spy_swap)
        monkeypatch.setattr(engine_instance, "apply_delta", _spy_apply)

        upd = client.put(
            "/api/sources/esj/config",
            json={"config": {"base_url": "https://www.esjzone.one"}},
        )
        assert upd.status_code == 200
        assert upd.json()["config"]["base_url"] == "https://www.esjzone.one"

        _assert_hydration("https://www.esjzone.one/forum/500/5001.html", expected_esj_sync=True)
        assert engine_calls == []
        assert app.state.query_engine is engine_instance

        # Disable ESJ again: offline browsing continues to work, can_sync flips to False.
        dis = client.put("/api/sources/esj/config", json={"enabled": False})
        assert dis.status_code == 200
        assert dis.json()["enabled"] is False

        esj_only = client.get("/api/novels", params={"keyword": "source:esj;", "order_by": "id"})
        assert esj_only.status_code == 200
        assert [n["id"] for n in esj_only.json()["novels"]] == [esj_nid]
        _assert_hydration("https://www.esjzone.one/forum/500/5001.html", expected_esj_sync=False)

        sync_blocked = client.post(f"/api/novels/{esj_nid}/sync")
        assert sync_blocked.status_code == 400
        assert "已禁用" in sync_blocked.json()["detail"]


# ---------------------------------------------------------------------------
# 5. GET /api/novels/author/search and GET /api/novels/series/search
# ---------------------------------------------------------------------------


def test_author_and_series_search_routes_and_filters(
    committed_session_factory,
    tmp_path: Path,
):
    """Author/series search routes avoid path collision and filter by substring, exact ID, and source."""
    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add_all(
            [
                models.Author(
                    author_id=-10, author_name="山田太郎_ESJ", source=SOURCE_ESJ, source_key="uid:10"
                ),
                models.Author(author_id=-11, author_name="無書作者", source=SOURCE_ESJ, source_key="uid:11"),
                models.Author(author_id=-12, author_name="本地山田", source=SOURCE_LOCAL, source_key="-12"),
                models.Author(author_id=200, author_name="山田Pixiv", source=SOURCE_PIXIV, source_key="200"),
            ]
        )
        uow.session.flush()
        uow.session.add_all(
            [
                models.Series(
                    series_id=-10,
                    series_name="異世界冒險_ESJ",
                    author_id=-10,
                    source=SOURCE_ESJ,
                    source_key="10",
                ),
                models.Series(
                    series_id=-11,
                    series_name="空系列_ESJ",
                    author_id=-10,
                    source=SOURCE_ESJ,
                    source_key="11",
                ),
                models.Series(
                    series_id=300,
                    series_name="異世界冒險_Pixiv",
                    author_id=200,
                    source=SOURCE_PIXIV,
                    source_key="300",
                ),
            ]
        )
        uow.session.flush()
        uow.session.add_all(
            [
                models.Novel(
                    id=-1001,
                    title="ESJ章1",
                    author_id=-10,
                    author_name="山田太郎_ESJ",
                    series_id=-10,
                    series_name="異世界冒險_ESJ",
                    series_index=1,
                    path=str(tmp_path / "download" / "esj" / "ESJ章1_-1001.txt"),
                    source=SOURCE_ESJ,
                    source_key="10/1001",
                ),
                models.Novel(
                    id=-1002,
                    title="ESJ章2",
                    author_id=-10,
                    author_name="山田太郎_ESJ",
                    series_id=-10,
                    series_name="異世界冒險_ESJ",
                    series_index=2,
                    path=str(tmp_path / "download" / "esj" / "ESJ章2_-1002.txt"),
                    source=SOURCE_ESJ,
                    source_key="10/1002",
                ),
                make_local_novel(
                    -1003,
                    title="本地書",
                    author_id=-12,
                    author_name="本地山田",
                ),
                models.Novel(
                    id=2001,
                    title="Pixiv章1",
                    author_id=200,
                    author_name="山田Pixiv",
                    series_id=300,
                    series_name="異世界冒險_Pixiv",
                    series_index=1,
                    path=str(tmp_path / "download" / "0000" / "Pixiv章1_2001.txt"),
                    source=SOURCE_PIXIV,
                    source_key="2001",
                ),
            ]
        )

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Author search does not collide with /{author_id}
        all_authors = client.get("/api/novels/author/search")
        assert all_authors.status_code == 200
        assert [(a["author_id"], a["novel_count"]) for a in all_authors.json()] == [
            (-10, 2),
            (-12, 1),
            (200, 1),
            (-11, 0),
        ]

        # Substring + source filter
        sub_authors = client.get("/api/novels/author/search", params={"q": "山田"})
        assert {a["author_id"] for a in sub_authors.json()} == {-10, -12, 200}

        esj_authors = client.get("/api/novels/author/search", params={"q": "山田", "source": SOURCE_ESJ})
        assert esj_authors.json() == [
            {
                "author_id": -10,
                "author_name": "山田太郎_ESJ",
                "source": SOURCE_ESJ,
                "novel_count": 2,
            }
        ]

        # Exact negative & positive numeric ID match
        neg_author = client.get("/api/novels/author/search", params={"q": "-10"})
        assert neg_author.status_code == 200
        assert neg_author.json()[0]["author_id"] == -10
        assert neg_author.json()[0]["novel_count"] == 2

        pos_author = client.get("/api/novels/author/search", params={"q": "200"})
        assert pos_author.status_code == 200
        assert pos_author.json() == [
            {
                "author_id": 200,
                "author_name": "山田Pixiv",
                "source": SOURCE_PIXIV,
                "novel_count": 1,
            }
        ]

        # Negative /{author_id} lookup also works cleanly
        lookup_neg_author = client.get("/api/novels/author/-10")
        assert lookup_neg_author.status_code == 200
        assert lookup_neg_author.json() == {
            "author_id": -10,
            "author_name": "山田太郎_ESJ",
            "source": "local",
            "novel_count": 2,
        }

        # Series search does not collide with /{series_id}
        all_series = client.get("/api/novels/series/search")
        assert all_series.status_code == 200
        assert [(s["series_id"], s["novel_count"]) for s in all_series.json()] == [
            (-10, 2),
            (300, 1),
            (-11, 0),
        ]

        sub_series = client.get("/api/novels/series/search", params={"q": "異世界冒險"})
        assert {s["series_id"] for s in sub_series.json()} == {-10, 300}

        pixiv_series = client.get(
            "/api/novels/series/search",
            params={"q": "異世界冒險", "source": SOURCE_PIXIV},
        )
        assert pixiv_series.json() == [
            {
                "series_id": 300,
                "series_name": "異世界冒險_Pixiv",
                "source": SOURCE_PIXIV,
                "author_id": 200,
                "novel_count": 1,
            }
        ]

        neg_series = client.get("/api/novels/series/search", params={"q": "-10"})
        assert neg_series.status_code == 200
        assert neg_series.json() == [
            {
                "series_id": -10,
                "series_name": "異世界冒險_ESJ",
                "source": SOURCE_ESJ,
                "author_id": -10,
                "novel_count": 2,
            }
        ]

        pos_series = client.get("/api/novels/series/search", params={"q": "300"})
        assert pos_series.status_code == 200
        assert pos_series.json()[0]["series_id"] == 300

        lookup_neg_series = client.get("/api/novels/series/-10")
        assert lookup_neg_series.status_code == 200
        assert lookup_neg_series.json() == {
            "series_id": -10,
            "series_name": "異世界冒險_ESJ",
            "source": "local",
            "novel_count": 2,
        }


# ---------------------------------------------------------------------------
# 6. Attaching local novels to ESJ entities, Pixiv isolation, delete & export
# ---------------------------------------------------------------------------


def test_attach_local_novels_to_esj_author_series_and_pixiv_series_repair_isolation(
    committed_session_factory,
    tmp_path: Path,
):
    """Local novels attach to negative ESJ author/series; ESJ series stay isolated from Pixiv repair."""
    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add_all(
            [
                models.Author(author_id=-1, author_name="ESJ名師", source=SOURCE_ESJ, source_key="uid:1"),
                models.Author(author_id=800, author_name="Pixiv作者", source=SOURCE_PIXIV, source_key="800"),
            ]
        )
        uow.session.flush()
        uow.session.add_all(
            [
                models.Series(
                    series_id=-1,
                    series_name="ESJ單章無號系列",
                    author_id=-1,
                    source=SOURCE_ESJ,
                    source_key="101",
                ),
                models.Series(
                    series_id=-2,
                    series_name="ESJ多章無號系列",
                    author_id=-1,
                    source=SOURCE_ESJ,
                    source_key="102",
                ),
                models.Series(
                    series_id=900,
                    series_name="Pixiv單章無號系列",
                    author_id=800,
                    source=SOURCE_PIXIV,
                    source_key="900",
                ),
            ]
        )
        uow.session.flush()
        uow.session.add_all(
            [
                models.Novel(
                    id=-1,
                    title="ESJ單章無號",
                    author_id=-1,
                    author_name="ESJ名師",
                    series_id=-1,
                    series_name="ESJ單章無號系列",
                    series_index=None,
                    path=str(tmp_path / "download" / "esj" / "ESJ單章無號_-1.txt"),
                    source=SOURCE_ESJ,
                    source_key="101/1",
                ),
                models.Novel(
                    id=-2,
                    title="ESJ多章無號A",
                    author_id=-1,
                    author_name="ESJ名師",
                    series_id=-2,
                    series_name="ESJ多章無號系列",
                    series_index=None,
                    path=str(tmp_path / "download" / "esj" / "ESJ多章無號A_-2.txt"),
                    source=SOURCE_ESJ,
                    source_key="102/1",
                ),
                models.Novel(
                    id=-3,
                    title="ESJ多章無號B",
                    author_id=-1,
                    author_name="ESJ名師",
                    series_id=-2,
                    series_name="ESJ多章無號系列",
                    series_index=None,
                    path=str(tmp_path / "download" / "esj" / "ESJ多章無號B_-3.txt"),
                    source=SOURCE_ESJ,
                    source_key="102/2",
                ),
                models.Novel(
                    id=8001,
                    title="Pixiv缺號書",
                    author_id=800,
                    author_name="Pixiv作者",
                    series_id=900,
                    series_name="Pixiv單章無號系列",
                    series_index=None,
                    path=str(tmp_path / "download" / "0000" / "Pixiv缺號書_8001.txt"),
                    source=SOURCE_PIXIV,
                    source_key="8001",
                ),
                make_local_novel(-10, title="本地番外1", author_id=None, author_name="未知作者"),
                make_local_novel(-11, title="本地番外2", author_id=None, author_name="未知作者"),
                make_local_novel(-12, title="本地番外3", author_id=None, author_name="未知作者"),
            ]
        )

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # 1) Single-novel PATCH attaches local novel to negative-ID ESJ author/series
        attach_ok = client.patch(
            "/api/novels/-10",
            json={"author_id": -1, "series_id": -1, "series_index": 2},
        )
        assert attach_ok.status_code == 200, attach_ok.text
        attached_body = attach_ok.json()
        assert attached_body["author_id"] == -1
        assert attached_body["author_name"] == "ESJ名師"
        assert attached_body["series_id"] == -1
        assert attached_body["series_name"] == "ESJ單章無號系列"
        assert attached_body["series_index"] == 2

        # Non-existent negative author/series rejected with 400
        bad_author = client.patch("/api/novels/-10", json={"author_id": -999999})
        assert bad_author.status_code == 400
        assert "不存在" in bad_author.json()["detail"]

        bad_series = client.patch("/api/novels/-10", json={"series_id": -999999})
        assert bad_series.status_code == 400
        assert "不存在" in bad_series.json()["detail"]

        # ESJ novels cannot edit title/author_id/series_id via PATCH (400)
        assert client.patch("/api/novels/-1", json={"title": "不允許改名"}).status_code == 400
        assert client.patch("/api/novels/-1", json={"author_id": -1}).status_code == 400

        # 2) batch_edit_metadata attaches multiple local novels while skipping ESJ & Pixiv rows
        batch_preview = client.post(
            "/api/novels/batch-metadata",
            json={"scope": {"novel_ids": [-11, -12, -1, 8001]}, "author_id": -1, "series_id": -1},
        )
        assert batch_preview.status_code == 200
        assert batch_preview.json() == {"matched": 4, "applicable": 2, "skipped": 2, "affected": 0}

        counts = asyncio.run(
            batch_edit_metadata(
                [-11, -12, -1, 8001],
                uow=uow,
                author_id=-1,
                series_id=-1,
                apply=True,
                source_registry=app.state.source_registry,
            )
        )
        assert counts == {"matched": 4, "applicable": 2, "skipped": 2, "affected": 2}

        with committed_session_factory() as session:
            for lid in (-11, -12):
                row = session.get(models.Novel, lid)
                assert row is not None
                assert row.author_id == -1
                assert row.author_name == "ESJ名師"
                assert row.series_id == -1
                assert row.series_name == "ESJ單章無號系列"

        # Non-existent negative author/series rejected cleanly in batch_edit_metadata
        with pytest.raises(ValidationError, match="不存在"):
            asyncio.run(
                batch_edit_metadata(
                    [-11, -12],
                    uow=uow,
                    author_id=-999999,
                    apply=True,
                    source_registry=app.state.source_registry,
                )
            )
        assert (
            client.post(
                "/api/novels/batch-metadata",
                json={"scope": {"novel_ids": [-11]}, "series_id": -999999, "apply": True},
            ).status_code
            == 400
        )

    # 3) Series repair queries & fix_series_index completely ignore ESJ series missing series_index
    with uow.begin(read_only=True):
        series_repo = SQLAlchemySeriesRepository(uow.session)
        assert series_repo.single_book_series_missing_index() == [8001]
        assert series_repo.series_with_empty_index() == [900]

    fake_pixiv = FakePixivClient()
    ctx = TaskContext(
        uow=uow,
        session_factory=committed_session_factory,
        client=fake_pixiv,  # type: ignore[arg-type]
    )
    repair_res = asyncio.run(fix_series_index(FixSeriesIndexArgs(), ctx))
    assert "单本系列补号 1 本" in repair_res.summary
    assert fake_pixiv.calls == []

    # Second run with only ESJ series still missing series_index -> zero work, zero API calls
    with uow.begin(read_only=True):
        series_repo = SQLAlchemySeriesRepository(uow.session)
        assert series_repo.single_book_series_missing_index() == []
        assert series_repo.series_with_empty_index() == []

    noop_res = asyncio.run(fix_series_index(FixSeriesIndexArgs(), ctx))
    assert noop_res.summary == "系列章节号检查: 无需修复"
    assert fake_pixiv.calls == []

    with committed_session_factory() as session:
        assert session.get(models.Novel, 8001).series_index == 1  # type: ignore[union-attr]
        for esj_nid in (-1, -2, -3):
            assert session.get(models.Novel, esj_nid).series_index is None  # type: ignore[union-attr]


def test_delete_esj_and_local_files_and_source_naming_template_batch_export(
    committed_session_factory,
    tmp_path: Path,
):
    """``delete_novel`` / ``delete_novel_files`` clean negative-ID files & assets; ``{source}`` works in export."""
    download_dir = tmp_path / "download"
    storage = FileStorage(str(download_dir))

    esj_txt = download_dir / "esj" / "ESJ導出章_-101.txt"
    esj_epub = esj_txt.with_suffix(".epub")
    esj_cover = download_dir / "esj" / "-101_c_cover.jpg"
    esj_img = download_dir / "esj" / "-101_u_1.png"
    local_txt = download_dir / "local" / "本地導出書_-102.txt"
    local_img = download_dir / "local" / "-102_u_1.png"
    pixiv_txt = download_dir / "0000" / "Pixiv導出書_103.txt"

    for p in (esj_txt, local_txt, pixiv_txt):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"正文:{p.stem}", encoding="utf-8")
    esj_epub.write_bytes(b"PK\x03\x04dummy-epub")
    esj_cover.write_bytes(_make_jpeg_bytes())
    esj_img.write_bytes(_make_png_bytes())
    local_img.write_bytes(_make_png_bytes())

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add_all(
            [
                models.Author(author_id=-101, author_name="ESJ作者", source=SOURCE_ESJ, source_key="uid:101"),
                models.Author(
                    author_id=1030, author_name="Pixiv作者", source=SOURCE_PIXIV, source_key="1030"
                ),
            ]
        )
        uow.session.flush()
        uow.session.add(
            models.Series(
                series_id=-101,
                series_name="ESJ系列",
                author_id=-101,
                source=SOURCE_ESJ,
                source_key="101",
            )
        )
        uow.session.flush()
        uow.session.add_all(
            [
                models.Novel(
                    id=-101,
                    title="ESJ導出章",
                    author_id=-101,
                    author_name="ESJ作者",
                    series_id=-101,
                    series_name="ESJ系列",
                    series_index=1,
                    path=str(esj_txt),
                    has_epub=EpubStatus.DONE,
                    source=SOURCE_ESJ,
                    source_key="101/1",
                ),
                make_local_novel(
                    -102,
                    title="本地導出書",
                    author_id=None,
                    author_name="本地作者",
                    path=str(local_txt),
                ),
                models.Novel(
                    id=103,
                    title="Pixiv導出書",
                    author_id=1030,
                    author_name="Pixiv作者",
                    path=str(pixiv_txt),
                    has_epub=EpubStatus.NO_IMAGES,
                    source=SOURCE_PIXIV,
                    source_key="103",
                ),
            ]
        )
        uow.session.flush()
        engine_delta.record_novel_upserts(uow.session, [-101, -102, 103])

    # Unit check on NovelNamingTemplate with {source}
    tmpl = NovelNamingTemplate("{source}/{author_name}/{title}_{id}")
    with uow.begin(read_only=True):
        esj_row = uow.session.get(models.Novel, -101)
        assert esj_row is not None
        assert tmpl.resolve(esj_row) == "esj/ESJ作者/ESJ導出章_-101"

    app = make_test_app(committed_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # Batch export preview & task with {source} placeholder
        preview = client.post(
            "/api/novels/batch-export/preview",
            json={
                "novel_ids": [-101],
                "format_mode": "prefer_epub",
                "naming_template": "{source}/{author_name}/{title}_{id}",
            },
        )
        assert preview.status_code == 200
        assert preview.json()["path"] == "esj/ESJ作者/ESJ導出章_-101.epub"

        export_req = client.post(
            "/api/novels/batch-export",
            json={
                "novel_ids": [-101, -102, 103],
                "format_mode": "prefer_epub",
                "naming_template": "{source}/{author_name}/{title}_{id}",
            },
        )
        assert export_req.status_code == 200
        task_id = export_req.json()["task_id"]

        ctx = TaskContext(
            uow=uow,
            file_storage=app.state.file_storage,
            task_id=task_id,
        )
        export_res = asyncio.run(
            batch_export(
                BatchExportArgs(
                    novel_ids=[-101, -102, 103],
                    format_mode="prefer_epub",
                    naming_template="{source}/{author_name}/{title}_{id}",
                ),
                ctx,
            )
        )
        assert "共 3 篇" in export_res.summary

        zip_dl = client.get(f"/api/novels/export/{task_id}/download")
        assert zip_dl.status_code == 200
        with zipfile.ZipFile(io.BytesIO(zip_dl.content)) as zf:
            assert set(zf.namelist()) == {
                "esj/ESJ作者/ESJ導出章_-101.epub",
                "local/本地作者/本地導出書_-102.txt",
                "pixiv/Pixiv作者/Pixiv導出書_103.txt",
            }

    # delete_novel removes ESJ DB row + .txt + .epub + assets cleanly
    delete_novel(-101, uow=uow, file_storage=storage)
    assert not esj_txt.exists()
    assert not esj_epub.exists()
    assert not esj_cover.exists()
    assert not esj_img.exists()
    assert not (download_dir / "esj").exists()

    # FileStorage.delete_novel_files cleans negative-ID local files + assets cleanly
    storage.delete_novel_files(str(local_txt))
    assert not local_txt.exists()
    assert not local_img.exists()
    assert not (download_dir / "local").exists()
    assert pixiv_txt.is_file()

    with committed_session_factory() as session:
        assert session.get(models.Novel, -101) is None
        assert session.get(models.Novel, 103) is not None
