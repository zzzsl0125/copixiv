"""``features/library/filenames.py`` —— 文件名 → (标题, 作者名)。

权威：``docs/LIBRARY_DESIGN.md`` §4-5（解析形态）与 §7-3（作者名非空）。

这套规则原来住在 ``ingest.py``，但它只关于"名字怎么拆"：改一种括号、加一个
分隔符的理由与改上传管线毫无共同之处。这里单独钉住形态表与那条不可动摇的
不变量（``author_name`` 永不为空）。
"""

from __future__ import annotations

import pytest

from copixiv.features.library.filenames import (
    UNKNOWN_AUTHOR,
    clean_filename,
    parse_filename,
)


class TestCleanFilename:
    def test_strips_client_side_directories(self):
        assert clean_filename("C:\\books\\我的书.txt") == "我的书.txt"
        assert clean_filename("books/我的书.txt") == "我的书.txt"

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_empty_becomes_a_txt_placeholder(self, raw):
        assert clean_filename(raw) == "未命名.txt"


class TestParseFilename:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("【作者】标题(完结).txt", ("标题", "作者")),
            ("【作者】标题.txt", ("标题", "作者")),
            ("[作者] 标题.txt", ("标题", "作者")),
            ("作者 - 标题.txt", ("标题", "作者")),
            ("作者 － 标题.txt", ("标题", "作者")),
            ("纯标题.txt", ("纯标题", UNKNOWN_AUTHOR)),
            ("标题（完结）.txt", ("标题", UNKNOWN_AUTHOR)),
            ("标题[全本].txt", ("标题", UNKNOWN_AUTHOR)),
            # 半角连字符不带空白不当分隔符 —— 否则标题被误拆成作者
            ("魔法少女-小圆.txt", ("魔法少女-小圆", UNKNOWN_AUTHOR)),
            # EPUB 后缀同样先剥掉
            ("【作者】标题.epub", ("标题", "作者")),
            # 无扩展名照旧解析
            ("没有扩展名", ("没有扩展名", UNKNOWN_AUTHOR)),
            # 客户端可能带上目录部分
            ("C:\\books\\【作者】标题.txt", ("标题", "作者")),
        ],
    )
    def test_forms(self, filename, expected):
        assert parse_filename(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        ["纯标题.txt", "...txt", "【】.txt", " - .txt", "（完结）.txt", ".txt"],
    )
    def test_author_is_never_empty(self, filename):
        """§3.3：``author_name`` 必须非 NULL，解析不出即「未知作者」。"""
        _, author = parse_filename(filename)
        assert author.strip() != ""
