"""Layer 1 unit tests: language filtering, NovelDraft builders, series order, and image markers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from copixiv.core.draft import (
    build_from_novel_info,
    build_from_webview,
    guess_series_order,
)
from copixiv.core.models import EpubStatus, NovelSource, TaskResult
from copixiv.core.text import (
    body_unchanged,
    filter_chinese,
    has_image_placeholders,
    has_image_placeholders_bytes,
    is_chinese,
)
from copixiv.core.time import month_ranges, parse_pixiv_time


def test_is_chinese_and_filter_chinese():
    assert is_chinese(title="「道歉」") is True
    assert is_chinese(title="海") is True
    assert is_chinese(title="日本語の小説", caption="テスト") is False
    assert is_chinese(title="日本語", tags=["中文"]) is True
    assert (
        is_chinese(
            title="第一章",
            caption="<p>Shop: https://example.com/abcdefghijklmnop</p>正文开始",
        )
        is True
    )
    assert is_chinese(title="Roll over", caption="English only lyrics") is False

    kept = filter_chinese(
        [
            {"title": "中文小说", "caption": "", "tags": []},
            {"title": "これは日本語", "caption": "", "tags": []},
            SimpleNamespace(title="English", caption="", tags=[SimpleNamespace(name="Chinese")]),
        ]
    )
    assert len(kept) == 2


def test_build_from_webview_and_novel_info():
    webview_payload = SimpleNamespace(
        id="1001",
        title="测试小说",
        user_id="88",
        rating=SimpleNamespace(bookmark=120, view=3400),
        caption="简介",
        series_id="500",
        series_title="测试系列",
        series_navigation={"prevNovel": {"contentOrder": 2}},
        cdate="2026-06-01T12:00:00+09:00",
        tags=["原创", {"name": "中文"}],
        text="正文内容 [uploadedimage:999]",
        images=[],  # Pixiv returns empty list when no uploaded images
        illusts={"ill1": {"url": "https://img/1.jpg"}},
        cover_url="https://img/cover.jpg",
    )
    draft = build_from_webview(webview_payload, download_dir="dl")
    assert draft.id == 1001
    assert draft.author_id == 88
    assert draft.like == 120
    assert draft.view == 3400
    assert draft.series_id == 500
    assert draft.series_index == 3
    assert draft.has_epub == EpubStatus.PENDING
    assert draft.images is None
    assert draft.illusts == {"ill1": {"url": "https://img/1.jpg"}}
    assert draft.tags == ["原创", "中文"]

    info_payload = SimpleNamespace(
        id=1002,
        title="元数据刷新",
        caption="cap",
        tags=[SimpleNamespace(name="TagA")],
        user=SimpleNamespace(id=77, name="作者甲"),
        series=SimpleNamespace(id=501, title="S", index=4),
        total_bookmarks=55,
        total_view=666,
        text_length=4321,
        create_date="2026-06-02T00:00:00+09:00",
    )
    info_draft = build_from_novel_info(info_payload)
    assert info_draft.author_name == "作者甲"
    assert info_draft.series_index == 4
    assert info_draft.has_epub is None
    assert info_draft.create_time == "2026-06-02"


def test_guess_series_order_and_time_and_placeholders():
    assert guess_series_order({"prevNovel": {"contentOrder": 5}}) == 6
    assert guess_series_order({"prevNovel": {}, "nextNovel": {"contentOrder": 3}}) == 2
    assert guess_series_order(None) is None

    dt_date = parse_pixiv_time("2026-01-02")
    assert dt_date is not None
    assert dt_date.tzinfo == UTC
    assert dt_date.hour == 15  # 00:00 JST == 15:00 previous day UTC
    assert parse_pixiv_time("") is None

    assert has_image_placeholders("hello [uploadedimage:123] world") is True
    assert has_image_placeholders("plain text") is False
    assert has_image_placeholders_bytes(b"prefix [pixivimage:456-1] suffix") is True
    assert has_image_placeholders_bytes(b"no markers here") is False


def test_text_language_symbols_body_hash_month_ranges_and_models():
    # Katakana middle dot / prolonged sound mark / double hyphen in Chinese titles are allowed
    assert is_chinese(title="阿斯托尔福・测试ー章节゠终章") is True
    assert body_unchanged("同一份正文", "同一份正文") is True
    assert body_unchanged("旧正文", "新正文") is False
    assert body_unchanged("", "") is False
    assert body_unchanged(None, "x") is False

    # month_ranges preserves tzinfo and rolls back cleanly across year boundary
    jst = timezone(timedelta(hours=9))
    end = datetime(2026, 2, 15, 18, 30, tzinfo=jst)
    windows = list(month_ranges(end, 4))
    assert windows == [
        (datetime(2026, 2, 1, 18, 30, tzinfo=jst), end),
        (datetime(2026, 1, 1, tzinfo=jst), datetime(2026, 1, 31, tzinfo=jst)),
        (datetime(2025, 12, 1, tzinfo=jst), datetime(2025, 12, 31, tzinfo=jst)),
        (datetime(2025, 11, 1, tzinfo=jst), datetime(2025, 11, 30, tzinfo=jst)),
    ]
    assert all(start.tzinfo == jst and stop.tzinfo == jst for start, stop in windows)

    # TaskResult.new_novel_count is a computed_field; NovelSource carries string values
    res = TaskResult(summary="ok", new_novel_titles=["甲", "乙"])
    assert res.new_novel_count == 2
    assert res.model_dump()["new_novel_count"] == 2
    assert NovelSource.PIXIV.value == "pixiv"
    assert NovelSource.LOCAL.value == "local"
