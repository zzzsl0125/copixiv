"""domain 层 Minor 问题复现（文件名引擎三漏洞 / parse_tags / series order / TaskResult）。"""

from copixiv.core.models import TaskResult
from copixiv.core.services import NovelNamingTemplate, _sanitize_path_segment
from copixiv.core.services import guess_series_order
from copixiv.core.services import parse_tags


# 文件名引擎 -------------------------------------------------------


def test_template_literal_illegal_chars_sanitized():
    """期望：模板字面量里的非法字符（如 ':'）也被清洗，产出 Windows 合法路径。"""
    from types import SimpleNamespace
    out = NovelNamingTemplate("{id}:{title}").resolve(
        SimpleNamespace(id=123, title="abc")
    )
    assert ":" not in out, f"路径含非法字符: {out!r}"


def test_dot_prefixed_reserved_names_detected():
    """期望：'.CON' / '..CON' 也被识别为 Windows 保留名。"""
    for seg in (".CON", "..CON"):
        out = _sanitize_path_segment(seg)
        assert "CON" not in out.upper() or "[" in out, f"{seg!r} → {out!r}"


def test_duplicate_empty_token_removed_everywhere():
    """期望：重复出现且值为空的 token，所有出现都删除，不残留字面量占位符。"""
    from types import SimpleNamespace
    out = NovelNamingTemplate("{title}_{id}_{title}").resolve(
        SimpleNamespace(id=123, title="")
    )
    assert "{title}" not in out, f"残留占位符: {out!r}"
    assert out == "123"


# parse_tags -------------------------------------------------------


def test_parse_tags_skips_none_entries():
    assert parse_tags([None]) == []
    assert parse_tags([{"name": "R-18"}, None, "中文"]) == ["r-18", "中文"]


# guess_series_order ------------------------------------------------


def test_guess_series_order_falls_back_to_next():
    """期望：prevNovel 存在但缺 contentOrder 时，回退尝试 nextNovel。"""
    nav = type("N", (), {
        "prevNovel": type("P", (), {"contentOrder": None})(),
        "nextNovel": type("X", (), {"contentOrder": 5})(),
    })()
    assert guess_series_order(nav) == 4  # 5 - 1


# TaskResult --------------------------------------------------------


def test_task_result_count_mirrors_titles():
    """期望：new_novel_count 始终镜像 titles 长度。"""
    r = TaskResult(summary="x", new_novel_titles=["a", "b"], new_novel_count=99)
    assert r.new_novel_count == 2
