"""EPUB 读取侧单测 —— ``storage/epub/reader.py``。

权威：``docs/LIBRARY_DESIGN.md`` §4.1（EPUB 上传）。

这里只测"字节流 → 正文 + 元数据"这一段：分派判据（后缀 / 内容嗅探）、
spine 顺序、HTML→纯文本（段落边界 / 实体 / 跳过 head·script·style）、
按文件自声明编码解码、以及坏容器 / 无正文两种失败的**区分**。落库、去重、
补挂不在本文件（``tests/features/test_library_ingest.py``）。
"""

from __future__ import annotations

import pytest

from copixiv.storage.epub import reader
from copixiv.storage.epub.reader import (
    EPUB_EMPTY_ERROR,
    EPUB_INVALID_ERROR,
    EPUB_TOO_LARGE_ERROR,
    EpubReadError,
    html_to_text,
    looks_like_epub,
    read_epub,
)
from tests.epub_samples import build_epub


class TestLooksLikeEpub:
    def test_epub_suffix_wins_without_sniffing(self):
        assert looks_like_epub("我的书.EPUB", b"not a zip at all")

    def test_content_sniffing_catches_a_renamed_file(self):
        data = build_epub(uid="renamed")
        assert looks_like_epub("我的书.bin", data) is True

    def test_txt_is_not_epub(self):
        assert looks_like_epub("我的书.txt", "正文".encode()) is False

    def test_a_plain_zip_is_not_epub(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "hi")
        assert looks_like_epub("伪装.epub", buffer.getvalue()) is True  # 后缀先命中
        assert looks_like_epub("伪装.bin", buffer.getvalue()) is False  # 内容不是


class TestReadEpub:
    def test_extracts_title_author_and_body(self):
        content = read_epub(build_epub(
            title="我的書名", author="某作者",
            chapters=(("第一段。", "第二段。"),),
        ))

        assert content.title == "我的書名"
        assert content.author_name == "某作者"
        assert content.body == "第一段。\n第二段。"

    def test_missing_metadata_is_none(self):
        content = read_epub(build_epub(title=None, author=None))

        assert content.title is None
        assert content.author_name is None

    def test_body_follows_spine_order(self):
        content = read_epub(build_epub(chapters=(
            ("第一章 开头。",), ("第二章 结尾。",),
        )))

        assert content.body == "第一章 开头。\n第二章 结尾。"

    def test_nav_and_head_title_do_not_leak_into_the_body(self):
        content = read_epub(build_epub(chapters=(("正文。",),)))

        assert content.body == "正文。"
        assert "导航标题不该进正文" not in content.body

    def test_text_html_manifest_entries_are_still_read(self):
        """不规范但真实：正文文档写成 ``text/html``（ebooklib 落成 EpubItem）。

        规范要求 ``application/xhtml+xml``；只认 ``EpubHtml`` 的话这类 EPUB 会被
        判成"没有可提取的正文"，而它其实读得出来。
        """
        import io

        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("text-html-case")
        book.set_title("t")
        book.add_item(epub.EpubItem(
            uid="ch1", file_name="ch1.html", media_type="text/html",
            content="<html><body><p>正文在 text/html 里。</p></body></html>".encode(),
        ))
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["ch1"]
        buffer = io.BytesIO()
        epub.write_epub(buffer, book)

        assert read_epub(buffer.getvalue()).body == "正文在 text/html 里。"

    def test_entities_are_unescaped_and_tags_dropped(self):
        content = read_epub(build_epub(
            chapters=(("第一段 &amp; 第二段",),),
            extra_body="<p>含 <b>加粗</b> 与 &lt;尖括号&gt;。</p>",
        ))

        assert content.body == "第一段 & 第二段\n含 加粗 与 <尖括号>。"

    def test_script_and_style_are_skipped(self):
        content = read_epub(build_epub(
            chapters=(("正文。",),),
            extra_body="<script>var x = 1;</script><style>p{color:red}</style>",
        ))

        assert content.body == "正文。"
        assert "var x" not in content.body
        assert "color" not in content.body

    def test_invalid_container_raises_invalid(self):
        with pytest.raises(EpubReadError) as excinfo:
            read_epub("这不是一个 EPUB，只是一串普通文字".encode())

        assert str(excinfo.value) == EPUB_INVALID_ERROR

    def test_container_without_body_text_raises_empty(self):
        with pytest.raises(EpubReadError) as excinfo:
            read_epub(build_epub(chapters=(("",),)))

        assert str(excinfo.value) == EPUB_EMPTY_ERROR

    def test_empty_bytes_raises_invalid(self):
        with pytest.raises(EpubReadError) as excinfo:
            read_epub(b"")

        assert str(excinfo.value) == EPUB_INVALID_ERROR

    def test_oversize_input_is_rejected_before_parsing(self, monkeypatch):
        """字节上限必须在**解析之前**拦 —— 否则畸形大容器会先被整个读进内存。"""
        monkeypatch.setattr(reader, "MAX_EPUB_BYTES", 8)
        with pytest.raises(EpubReadError) as excinfo:
            read_epub(build_epub())

        assert str(excinfo.value) == EPUB_TOO_LARGE_ERROR


class TestHtmlToText:
    def test_block_tags_become_line_breaks(self):
        markup = b"<html><body><p>a</p><div>b</div><br/>c</body></html>"

        assert html_to_text(markup) == "a\nb\nc"

    def test_inline_whitespace_is_collapsed(self):
        markup = "<p>前    后\t并排</p>".encode()

        assert html_to_text(markup) == "前 后 并排"

    def test_file_declared_encoding_is_honoured(self):
        # XML 声明说 gb2312，字节确实是 GB18030/Big5 那一族 —— 自声明优先，
        # 否则这段中文会被 UTF-8 解码成替换符。
        markup = (
            '<?xml version="1.0" encoding="gb2312"?>'
            "<html><body><p>简体中文正文</p></body></html>"
        ).encode("gb18030")

        assert html_to_text(markup) == "简体中文正文"

    def test_broken_bytes_degrade_to_replacement_not_crash(self):
        markup = b"<p>" + b"\xff\xfe\xfa" + b"</p>"

        # 只要求不抛异常且拿回字符串；坏字节按替换符处理。
        assert isinstance(html_to_text(markup), str)
