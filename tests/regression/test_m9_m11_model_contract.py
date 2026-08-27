"""M9/M11 复现：领域模型双轨漂移 + text=None 崩溃。

M9 期望（Pydantic 统一）：
- Novel.create_time 标注为 str | None（与 DB/String 一致）
- 工厂函数返回 Novel 实例（带瞬态字段）
- 标签键统一为模型字段 ``tags``（不再有 dict 键 "tag"）
- build_from_novel_info 对 has_epub 用 None 表示「不覆盖」

M11 期望：webview 响应缺失 text 时不崩溃。
"""

from copixiv.core.models import Novel, EpubStatus
from copixiv.core.services import (
    build_novel, build_from_novel_info, build_from_webview,
)


class _User:
    id = 7
    name = "作者七"


class _Series:
    id = 3
    title = "系列"
    index = 2


class _NovelInfo:
    id = 123
    title = "标题"
    caption = "简介"
    tags = [{"name": "R-18"}, {"name": "中文"}]
    user = _User()
    series = _Series()
    total_bookmarks = 100
    total_view = 200
    text_length = 3000
    create_date = "2026-05-01T12:00:00+09:00"


class _Webview:
    id = 123
    title = "标题"
    user_id = 7
    rating = type("R", (), {"bookmark": 1, "view": 2})()
    text = "正文内容"
    caption = "简介"
    series_id = 3
    series_title = "系列"
    series_navigation = None
    cdate = "2026-05-01T12:00:00+09:00"
    tags = [{"name": "中文"}]
    images = {}
    illusts = {}
    cover_url = None


# M9 -------------------------------------------------------------


def test_novel_create_time_annotation_is_str():
    """模型标注必须与 DB（String 列）一致：str | None。"""
    from typing import get_type_hints
    assert get_type_hints(Novel)["create_time"] == str | None


def test_build_novel_returns_novel_model():
    novel = build_novel(id=1, title="t", author_id=2)
    assert isinstance(novel, Novel)
    # 瞬态字段在模型上
    assert novel.content is None
    # 标签字段名是 tags（模型字段），不是 dict 键 "tag"
    novel2 = build_novel(id=2, title="t2", author_id=2, tags=["R-18"])
    assert novel2.tags == ["r-18"]  # parse_tags 统一小写
    assert "tag" not in novel2.model_dump()


def test_build_from_novel_info_returns_model_with_epub_none():
    novel = build_from_novel_info(_NovelInfo())
    assert isinstance(novel, Novel)
    # 「不覆盖」语义：has_epub 为 None，而非整个键消失
    assert novel.has_epub is None
    assert novel.tags == ["r-18", "中文"]


def test_novel_roundtrip_via_model_dump():
    """仓储可安全地用 model_dump() 拿字段（transient 字段也在，但按白名单过滤）。"""
    novel = build_from_webview(_Webview())
    d = novel.model_dump()
    assert d["id"] == 123
    assert d["create_time"] == "2026-05-01T12:00:00+09:00"
    assert d["tags"] == ["中文"]
    assert d["has_epub"] == EpubStatus.NO  # 正文无图片占位符


# M11 -------------------------------------------------------------


def test_build_from_webview_text_none_does_not_crash():
    wv = _Webview()
    wv.text = None
    novel = build_from_webview(wv)  # 当前实现：len(None) → TypeError
    assert novel.text == 0
    assert novel.has_epub == EpubStatus.NO


# M14 -------------------------------------------------------------


def test_build_from_webview_images_illusts_empty_list_normalised():
    """webview API 对无图小说返回 images/illusts 为 []（而非 dict）。

    曾导致 Novel 校验崩溃：ValidationError (images/illusts
    Input should be a valid dictionary)。工厂应把非 dict 归一化为 None。
    """
    wv = _Webview()
    wv.images = []
    wv.illusts = []
    novel = build_from_webview(wv)
    assert novel.images is None
    assert novel.illusts is None


def test_build_from_webview_images_dict_preserved():
    """dict 形式的 images/illusts 原样保留（有图小说的正常路径）。"""
    wv = _Webview()
    wv.images = {"1": {"urls": {"original": "http://x/a.jpg"}}}
    novel = build_from_webview(wv)
    assert novel.images == {"1": {"urls": {"original": "http://x/a.jpg"}}}
