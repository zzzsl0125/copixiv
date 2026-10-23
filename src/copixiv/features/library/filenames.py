"""文件名 → (标题, 作者名) 的解析规则（``docs/LIBRARY_DESIGN.md`` §4-5）。

纯字符串处理，没有 I/O。独立成模块是因为这套规则**只关于"名字"**：解码、
落库、去重都不需要知道 ``【作者】标题(完结).txt`` 该怎么拆，而"怎么拆"会为了
各种下载站的命名习惯单独演化（加一种括号、改一个分隔符），改它的理由与改
上传管线完全不同。

契约里唯一不可动摇的一条：**``author_name`` 永不为空**。解析不出就是
「未知作者」——§3.3 的产品承诺「每本书都有作者名可显示」在入库侧的落点，且
不依赖任何任务（``sync_empty_name`` 默认禁用，见 §7-3）。
"""

from __future__ import annotations

import re

UNKNOWN_AUTHOR = "未知作者"

# ``【作者】标题`` / ``[作者] 标题`` —— 方括号（中英文）打头即作者。
_BRACKET_RE = re.compile(
    r"^\s*[【\[]\s*(?P<author>[^】\]]+?)\s*[】\]]\s*(?P<title>.+?)\s*$"
)

# ``作者 - 标题`` 的分隔符：半角/全角连字符**两侧要有空白**，破折号（– —）
# 可无空白。半角 ``-`` 不带空白时不当分隔符 —— 否则
# ``魔法少女-小圆.txt`` 会被误拆成作者「魔法少女」。
_DASH_SEPARATORS = (" - ", " － ", " – ", " — ", " –", "—", "–")

# 「(完结)」这类尾部状态标记：只有括号内容命中已知标记才剥掉，避免误删标题。
_STATUS_MARKERS = frozenset({
    "完结", "已完结", "完结文", "全本", "全集", "完", "连载", "未完结",
    "txt", "精校", "校对",
})
_TRAILING_GROUP_RE = re.compile(r"[（(\[【]\s*([^）)\]】]{0,16}?)\s*[）)\]】]\s*$")

# 上传文件名可能带上的两种后缀（先剥掉再解析）。
_KNOWN_SUFFIXES = (".txt", ".epub")


def clean_filename(filename: str) -> str:
    """取出文件名本体（去掉上传客户端可能带上的目录部分）。"""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "未命名.txt"


def _strip_status_markers(title: str) -> str:
    """剥掉尾部的 ``(完结)`` / ``（全本）`` / ``[txt]`` 一类状态标记。"""
    current = title.strip()
    while True:
        match = _TRAILING_GROUP_RE.search(current)
        if not match:
            return current
        if match.group(1).strip().lower() not in _STATUS_MARKERS:
            return current
        stripped = current[: match.start()].strip()
        if not stripped:
            return current
        current = stripped


def _split_dash(stem: str) -> tuple[str, str] | None:
    """``作者 - 标题`` → ``(author, title)``；没有分隔符返回 None。"""
    for separator in _DASH_SEPARATORS:
        index = stem.find(separator)
        if index <= 0:
            continue
        author = stem[:index].strip()
        title = stem[index + len(separator):].strip()
        if author and title:
            return author, title
    return None


def parse_filename(filename: str) -> tuple[str, str]:
    """从文件名解析 ``(title, author_name)``。

    支持 ``【作者】标题(完结).txt`` / ``[作者] 标题.txt`` / ``作者 - 标题.txt``
    / 纯 ``标题.txt``（§4-5；``.epub`` 后缀同样先剥掉）。**``author_name``
    永不为空**：解析不出即「未知作者」——§3.3 产品承诺「每本书都有作者名可
    显示」在入库侧的落点，不依赖任何任务（``sync_empty_name`` 默认禁用）。
    EPUB 上传时它只是**兜底**：``dc:title`` / ``dc:creator`` 才是首选来源
    （见 :func:`copixiv.features.library.ingest.resolve_metadata`）。
    """
    stem = clean_filename(filename)
    lower = stem.lower()
    for suffix in _KNOWN_SUFFIXES:
        if lower.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem.strip()

    author = ""
    title = stem
    match = _BRACKET_RE.match(stem)
    if match:
        author, title = match.group("author"), match.group("title")
    else:
        split = _split_dash(stem)
        if split:
            author, title = split

    title = _strip_status_markers(title).strip() or title.strip() or stem
    return title, (author.strip() or UNKNOWN_AUTHOR)


__all__ = ["UNKNOWN_AUTHOR", "clean_filename", "parse_filename"]
