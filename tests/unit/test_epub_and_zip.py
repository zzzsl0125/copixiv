"""Layer 1 unit tests: EPUB builder/reader, XXE & size guards, state predicates, and batch ZIP."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from copixiv.core.draft import build_novel
from copixiv.core.models import EpubStatus
from copixiv.storage.epub import reader, state
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.zip import build_batch_zip


def _make_minimal_epub(
    *,
    container_xml: bytes | None = None,
    opf_xml: bytes | None = None,
    chapters: dict[str, bytes] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            container_xml
            or (
                b'<?xml version="1.0"?>'
                b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                b"<rootfiles>"
                b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                b"</rootfiles>"
                b"</container>"
            ),
        )
        zf.writestr(
            "OEBPS/content.opf",
            opf_xml
            or (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                b"<dc:title>Test Book</dc:title>"
                b"<dc:creator>Author X</dc:creator>"
                b"</metadata>"
                b"<manifest>"
                b'<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                b'<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
                b"</manifest>"
                b"<spine><itemref idref=\"c2\"/><itemref idref=\"c1\"/></spine>"
                b"</package>"
            ),
        )
        for name, content in (
            chapters
            or {
                "OEBPS/ch1.xhtml": b"<html><body><p>Second in spine</p></body></html>",
                "OEBPS/ch2.xhtml": b"<html><body><p>First in spine</p></body></html>",
            }
        ).items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_epub_builder_transcodes_transparent_png_and_degrades_missing_images(tmp_path: Path):
    novel_id = 8001
    txt_path = tmp_path / f"story_{novel_id}.txt"
    txt_path.write_text("unused", encoding="utf-8")

    # Palette/RGBA PNG on disk for image 111; image 222 intentionally missing
    png_path = tmp_path / f"{novel_id}_u_111.png"
    img = Image.new("RGBA", (16, 16), (255, 0, 0, 128))
    img.save(png_path, format="PNG")

    draft = build_novel(
        id=novel_id,
        title="插图测试",
        author_id=10,
        author_name="作者",
        content="第一段\n[uploadedimage:111]\n第二段\n[pixivimage:222-1]",
        images={"111": {"url": "x"}},
        illusts={"222-1": {"url": "y"}},
        download_dir=str(tmp_path),
    )
    # Point draft.path directly into tmp_path
    draft = build_novel(
        id=novel_id,
        title="插图测试",
        author_id=10,
        author_name="作者",
        content=draft.content,
        images=draft.images,
        illusts=draft.illusts,
        download_dir=str(tmp_path),
    )
    # Override path by writing into built path
    target_txt = Path(draft.path)
    target_txt.parent.mkdir(parents=True, exist_ok=True)
    target_txt.write_text(draft.content or "", encoding="utf-8")
    (target_txt.parent / f"{novel_id}_u_111.png").write_bytes(png_path.read_bytes())

    assert EpubBuilder().create_epub(draft) is True
    epub_path = target_txt.with_suffix(".epub")
    assert state.is_readable_container(epub_path) is True
    assert state.has_embedded_images(epub_path) is True
    assert state.has_raw_markers(epub_path) is False
    assert state.is_complete(epub_path) is True

    with zipfile.ZipFile(epub_path) as zf:
        xhtml = zf.read("EPUB/content.xhtml").decode("utf-8")
        assert "illust-missing" in xhtml
        assert "[uploadedimage:111]" not in xhtml
        assert "[pixivimage:222-1]" not in xhtml


def test_epub_reader_reads_spine_order_and_blocks_xxe_and_size(monkeypatch):
    valid = _make_minimal_epub()
    parsed = reader.read_epub(valid)
    assert parsed.title == "Test Book"
    assert parsed.author_name == "Author X"
    assert parsed.body.index("First in spine") < parsed.body.index("Second in spine")

    # XXE attempt in OPF must not resolve external entity
    xxe_opf = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE package [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>&xxe;</dc:title>"
        b"</metadata>"
        b"<manifest><item id=\"c1\" href=\"ch1.xhtml\" media-type=\"application/xhtml+xml\"/></manifest>"
        b"<spine><itemref idref=\"c1\"/></spine>"
        b"</package>"
    )
    xxe_epub = _make_minimal_epub(opf_xml=xxe_opf)
    xxe_parsed = reader.read_epub(xxe_epub)
    assert "root:" not in (xxe_parsed.title or "")

    # Oversize container check
    monkeypatch.setattr(reader, "MAX_CONTAINER_BYTES", 32)
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_TOO_LARGE_ERROR):
        reader.read_epub(valid)


def test_build_batch_zip_formats_missing_and_progress(tmp_path: Path):
    txt1 = tmp_path / "one_1.txt"
    txt1.write_text("txt1", encoding="utf-8")
    epub1 = tmp_path / "one_1.epub"
    epub1.write_bytes(b"epub1")

    txt2 = tmp_path / "two_2.txt"
    txt2.write_text("txt2", encoding="utf-8")

    novels = [
        SimpleNamespace(
            id=1, path=str(txt1), title="One", author_name="A",
            series_id=None, series_name=None, series_index=None,
            has_epub=EpubStatus.DONE,
        ),
        SimpleNamespace(
            id=2, path=str(txt2), title="Two", author_name="A",
            series_id=None, series_name=None, series_index=None,
            has_epub=EpubStatus.PENDING,  # not DONE -> falls back to txt
        ),
        SimpleNamespace(
            id=3, path=str(tmp_path / "missing_3.txt"), title="Three", author_name="A",
            series_id=None, series_name=None, series_index=None,
            has_epub=EpubStatus.NO_IMAGES,
        ),
    ]
    progress_calls: list[tuple[int, int]] = []
    buf, added, missing = build_batch_zip(
        novels,
        format_mode="prefer_epub",
        naming_template="{author_name}/{title}_{id}",
        progress=lambda done, total: progress_calls.append((done, total)),
    )
    try:
        assert added == ["One", "Two"]
        assert missing == ["3"]
        assert progress_calls == []  # _PROGRESS_EVERY is 500
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            names = set(zf.namelist())
            assert names == {"A/One_1.epub", "A/Two_2.txt"}
    finally:
        buf.close()
