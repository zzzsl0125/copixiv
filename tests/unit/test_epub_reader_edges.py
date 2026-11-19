"""Layer 1 unit tests: EPUB reader edge cases, error guards, metadata, and member size limits."""

from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.factories import make_minimal_epub

from copixiv.storage.epub import reader


def test_looks_like_epub_by_suffix_and_zip_mimetype():
    assert reader.looks_like_epub("book.EPUB", b"anything") is True
    assert reader.looks_like_epub("book.txt", b"plain text") is False

    renamed_epub = make_minimal_epub()
    assert reader.looks_like_epub("renamed.bin", renamed_epub) is True

    plain_zip_buf = io.BytesIO()
    with zipfile.ZipFile(plain_zip_buf, "w") as zf:
        zf.writestr("hello.txt", "world")
    assert reader.looks_like_epub("not_epub.zip", plain_zip_buf.getvalue()) is False


def test_read_epub_rejects_corrupt_missing_or_empty_containers():
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_INVALID_ERROR):
        reader.read_epub(b"")

    with pytest.raises(reader.EpubReadError, match=reader.EPUB_INVALID_ERROR):
        reader.read_epub(b"not a zip archive")

    # Missing container.xml
    no_container = io.BytesIO()
    with zipfile.ZipFile(no_container, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_INVALID_ERROR):
        reader.read_epub(no_container.getvalue())

    # Valid container + OPF but empty body text
    empty_body = make_minimal_epub(
        chapters={
            "OEBPS/ch1.xhtml": b"<html><body><script>var x=1;</script><style>p{}</style></body></html>",
            "OEBPS/ch2.xhtml": b"<html><body>   </body></html>",
        }
    )
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_EMPTY_ERROR):
        reader.read_epub(empty_body)


def test_read_epub_prefers_aut_role_normalizes_paths_and_truncates_metadata():
    long_title = "标题" * 150
    opf = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf" version="2.0">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>  &lt;b&gt;{long_title}&lt;/b&gt; &amp; 副标题 </dc:title>"
        f'<dc:creator opf:role="trl">Translator</dc:creator>'
        f'<dc:creator opf:role="aut">Real Author</dc:creator>'
        f"</metadata>"
        f"<manifest>"
        f'<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
        f'<item id="ch" href="../Text/chap1.xhtml#frag" media-type="application/xhtml+xml"/>'
        f"</manifest>"
        f'<spine><itemref idref="nav"/><itemref idref="ch"/></spine>'
        f"</package>"
    ).encode()
    epub = make_minimal_epub(
        opf_xml=opf,
        chapters={
            "OEBPS/nav.xhtml": b"<html><body><p>NAV SHOULD BE SKIPPED</p></body></html>",
            "Text/chap1.xhtml": (
                b"<html><head><title>Ignored</title></head>"
                b"<body><script>ignored()</script><p>Line 1<br/>Line 2</p><p>Para &quot;B&quot;</p></body></html>"
            ),
        },
    )
    content = reader.read_epub(epub)
    assert content.author_name == "Real Author"
    assert len(content.title or "") == reader.MAX_METADATA_CHARS
    assert "NAV SHOULD BE SKIPPED" not in content.body
    assert "ignored()" not in content.body
    assert content.body == 'Line 1\nLine 2\nPara "B"'


def test_read_epub_enforces_member_size_guard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader, "_MAX_MEMBER_BYTES", 32)
    oversized_member = make_minimal_epub(
        chapters={
            "OEBPS/ch1.xhtml": b"<html><body><p>" + b"A" * 100 + b"</p></body></html>",
            "OEBPS/ch2.xhtml": b"<html><body><p>ok</p></body></html>",
        }
    )
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_TOO_LARGE_ERROR):
        reader.read_epub(oversized_member)


def test_read_epub_concurrent_and_cumulative_byte_bounds(monkeypatch: pytest.MonkeyPatch):
    epub = make_minimal_epub(
        chapters={
            "OEBPS/ch1.xhtml": b"<html><body><p>Alpha</p></body></html>",
            "OEBPS/ch2.xhtml": b"<html><body><p>Beta</p></body></html>",
        }
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reader.read_epub, [epub] * 12))
    assert all("Alpha" in r.body and "Beta" in r.body for r in results)

    # Oversized mimetype in looks_like_epub does not read unbounded bytes
    huge_mime_buf = io.BytesIO()
    with zipfile.ZipFile(huge_mime_buf, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip" + b" " * 200)
    assert reader.looks_like_epub("sneaky.bin", huge_mime_buf.getvalue()) is False

    # Oversized container.xml rejected
    monkeypatch.setattr(reader, "_MAX_CONTAINER_XML_BYTES", 32)
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_TOO_LARGE_ERROR):
        reader.read_epub(epub)
    monkeypatch.undo()

    # Multi-chapter cumulative byte limit rejects bomb spread across small chapters
    monkeypatch.setattr(reader, "_MAX_TOTAL_UNCOMPRESSED_BYTES", 60)
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_TOO_LARGE_ERROR):
        reader.read_epub(epub)
    monkeypatch.undo()

    # Non-UTF-8 (gbk) OPF and Big5/GB18030 XHTML chapters parse cleanly via charset-normalizer
    gbk_opf = (
        '<?xml version="1.0" encoding="gbk"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>简体书名</dc:title><dc:creator>简体作者</dc:creator>"
        "</metadata><manifest>"
        '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'
    ).encode("gbk")
    big5_chapter = "<html><body><p>這是繁體中文小說正文內容，用來測試編碼偵測。</p></body></html>".encode(
        "big5"
    )
    gb_chapter = "<html><body><p>这是简体中文小说正文内容，用来测试编码侦测。</p></body></html>".encode(
        "gb18030"
    )
    multibyte_epub = make_minimal_epub(
        opf_xml=gbk_opf,
        chapters={"OEBPS/ch1.xhtml": big5_chapter, "OEBPS/ch2.xhtml": gb_chapter},
    )
    parsed_mb = reader.read_epub(multibyte_epub)
    assert parsed_mb.title == "简体书名"
    assert parsed_mb.author_name == "简体作者"
    assert "這是繁體中文小說正文內容" in parsed_mb.body
    assert "这是简体中文小说正文内容" in parsed_mb.body
