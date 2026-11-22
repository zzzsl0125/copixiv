"""Layer 1 unit tests: EPUB3 builder/reader, XXE & size guards, state predicates, and batch ZIP."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from tests.factories import make_minimal_epub

from copixiv.core.draft import build_novel
from copixiv.core.models import EpubStatus
from copixiv.storage.epub import reader, state
from copixiv.storage.epub.builder import EpubBuilder, EpubBuildResult
from copixiv.storage.zip import build_batch_zip, build_batch_zip_to_file


def test_epub_builder_epub3_structure_spine_roundtrip_and_missing_images(tmp_path: Path):
    novel_id = 8001

    draft = build_novel(
        id=novel_id,
        title="插图测试 & 卷1",
        author_id=10,
        author_name="作者 & 甲",
        content="第一段\n[uploadedimage:111]\n第二段\n[pixivimage:222-1]",
        images={"111": {"url": "x"}},
        illusts={"222-1": {"url": "y"}},
        download_dir=str(tmp_path),
    )
    target_txt = Path(draft.path)
    target_txt.parent.mkdir(parents=True, exist_ok=True)
    target_txt.write_text(draft.content or "", encoding="utf-8")
    # Palette/RGBA PNG on disk for image 111 + cover; image 222 intentionally missing
    Image.new("RGBA", (16, 16), (255, 0, 0, 128)).save(
        target_txt.parent / f"{novel_id}_u_111.png",
        format="PNG",
    )
    Image.new("RGB", (16, 16), (0, 128, 255)).save(
        target_txt.parent / f"{novel_id}_c_cover.jpg",
        format="JPEG",
    )

    res = EpubBuilder().create_epub(draft)
    assert isinstance(res, EpubBuildResult)
    assert bool(res) is True
    assert res.ok is True
    assert res.written is True
    assert res.embedded_images == 1
    assert res.missing_images == 1
    assert res.has_raw_markers is False
    assert res.has_cover is True
    assert res.is_complete is True

    epub_path = target_txt.with_suffix(".epub")
    assert res.output_path == epub_path
    assert state.inspect_epub(epub_path) == (True, True)

    with zipfile.ZipFile(epub_path) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        assert zf.getinfo("EPUB/cover.jpg").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("EPUB/images/111.jpg").compress_type == zipfile.ZIP_STORED

        opf_root = ET.fromstring(zf.read("EPUB/content.opf"))
        spine_refs = [el.get("idref") for el in opf_root.iter("{http://www.idpf.org/2007/opf}itemref")]
        # Cover page precedes main content; nav is NOT in spine
        assert spine_refs == ["cover-page", "content"]

        xhtml = zf.read("EPUB/content.xhtml").decode("utf-8")
        assert "illust-missing" in xhtml
        assert "[uploadedimage:111]" not in xhtml
        assert "[pixivimage:222-1]" not in xhtml
        content_info = zf.getinfo("EPUB/content.xhtml")

    # Builder -> Reader round-trip preserves metadata and body
    parsed_roundtrip = reader.read_epub(epub_path.read_bytes())
    assert parsed_roundtrip.title == "插图测试 & 卷1"
    assert parsed_roundtrip.author_name == "作者 & 甲"
    assert "第一段" in parsed_roundtrip.body
    assert "第二段" in parsed_roundtrip.body

    # Corrupt compressed payload of EPUB/content.xhtml -> state.inspect_epub returns (True, False)
    raw_bytes = bytearray(epub_path.read_bytes())
    hdr = content_info.header_offset
    fn_len = int.from_bytes(raw_bytes[hdr + 26 : hdr + 28], "little")
    extra_len = int.from_bytes(raw_bytes[hdr + 28 : hdr + 30], "little")
    raw_bytes[hdr + 30 + fn_len + extra_len] ^= 0xFF
    corrupt_epub = tmp_path / "corrupt.epub"
    corrupt_epub.write_bytes(bytes(raw_bytes))
    assert state.inspect_epub(corrupt_epub) == (True, False)


def test_epub_reader_reads_spine_order_and_blocks_xxe_and_size(monkeypatch):
    valid = make_minimal_epub()
    parsed = reader.read_epub(valid)
    assert parsed.title == "Test Book"
    assert parsed.author_name == "Author X"
    assert parsed.body.index("First in spine") < parsed.body.index("Second in spine")

    # XXE attempt in OPF is rejected cleanly by stdlib ElementTree
    xxe_opf = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE package [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>&xxe;</dc:title>"
        b"</metadata>"
        b'<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        b'<spine><itemref idref="c1"/></spine>'
        b"</package>"
    )
    xxe_epub = make_minimal_epub(opf_xml=xxe_opf)
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_INVALID_ERROR):
        reader.read_epub(xxe_epub)

    # Oversize container check
    monkeypatch.setattr(reader, "MAX_CONTAINER_BYTES", 32)
    with pytest.raises(reader.EpubReadError, match=reader.EPUB_TOO_LARGE_ERROR):
        reader.read_epub(valid)


def test_build_batch_zip_formats_compression_root_guard_and_progress(tmp_path: Path):
    dl_root = tmp_path / "dl"
    dl_root.mkdir()
    outside = tmp_path / "secret_4.txt"
    outside.write_text("do not leak", encoding="utf-8")

    txt1 = dl_root / "one_1.txt"
    txt1.write_text("txt1", encoding="utf-8")
    epub1 = dl_root / "one_1.epub"
    epub1.write_bytes(b"epub1")

    txt2 = dl_root / "two_2.txt"
    txt2.write_text("txt2", encoding="utf-8")

    novels = [
        SimpleNamespace(
            id=1,
            path=str(txt1),
            title="One",
            author_name="A",
            series_id=None,
            series_name=None,
            series_index=None,
            has_epub=EpubStatus.DONE,
        ),
        SimpleNamespace(
            id=2,
            path=str(txt2),
            title="Two",
            author_name="A",
            series_id=None,
            series_name=None,
            series_index=None,
            has_epub=EpubStatus.PENDING,  # not DONE -> falls back to txt
        ),
        SimpleNamespace(
            id=3,
            path=str(dl_root / "missing_3.txt"),
            title="Three",
            author_name="A",
            series_id=None,
            series_name=None,
            series_index=None,
            has_epub=EpubStatus.NO_IMAGES,
        ),
        SimpleNamespace(
            id=4,
            path=str(outside),
            title="Escape",
            author_name="A",
            series_id=None,
            series_name=None,
            series_index=None,
            has_epub=EpubStatus.NO_IMAGES,
        ),
    ]
    progress_calls: list[tuple[int, int]] = []
    buf, added, missing = build_batch_zip(
        novels,
        format_mode="prefer_epub",
        naming_template="{author_name}/{title}_{id}",
        progress=lambda done, total: progress_calls.append((done, total)),
        download_root=dl_root,
    )
    try:
        assert added == 2
        assert missing == ["3", "4"]
        assert progress_calls == []
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            names = set(zf.namelist())
            assert names == {"A/One_1.epub", "A/Two_2.txt"}
            assert zf.getinfo("A/One_1.epub").compress_type == zipfile.ZIP_STORED
            assert zf.getinfo("A/Two_2.txt").compress_type == zipfile.ZIP_DEFLATED
    finally:
        buf.close()


def test_build_batch_zip_to_file_direct_and_empty_guard(tmp_path: Path):
    dl_root = tmp_path / "dl"
    dl_root.mkdir()
    txt1 = dl_root / "one_1.txt"
    txt1.write_text("txt1", encoding="utf-8")

    novel_ok = SimpleNamespace(
        id=1,
        path=str(txt1),
        title="One",
        author_name="A",
        series_id=None,
        series_name=None,
        series_index=None,
        has_epub=EpubStatus.NO_IMAGES,
    )
    novel_missing = SimpleNamespace(
        id=2,
        path=str(dl_root / "missing_2.txt"),
        title="Missing",
        author_name="A",
        series_id=None,
        series_name=None,
        series_index=None,
        has_epub=EpubStatus.NO_IMAGES,
    )

    out_ok = tmp_path / "batch_export_1.zip"
    size_bytes, added, missing = build_batch_zip_to_file(
        [novel_ok, novel_missing],
        out_ok,
        format_mode="txt",
        naming_template="{title}_{id}",
        download_root=dl_root,
    )
    assert size_bytes > 0
    assert out_ok.is_file()
    assert added == 1
    assert missing == ["2"]
    assert not list(tmp_path.glob(".*.tmp"))
    with zipfile.ZipFile(out_ok) as zf:
        assert zf.namelist() == ["One_1.txt"]

    # Empty package: 0 valid files -> no output file created, size_bytes == 0
    out_empty = tmp_path / "batch_export_2.zip"
    size_empty, added_empty, missing_empty = build_batch_zip_to_file(
        [novel_missing],
        out_empty,
        format_mode="txt",
        naming_template="{title}_{id}",
        download_root=dl_root,
    )
    assert size_empty == 0
    assert added_empty == 0
    assert missing_empty == ["2"]
    assert not out_empty.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_epub_builder_zero_embedded_images_does_not_overwrite_existing_epub(tmp_path: Path):
    novel_id = 8002
    draft = build_novel(
        id=novel_id,
        title="缺图保护",
        author_id=10,
        author_name="作者",
        content="正文\n[uploadedimage:111]",
        images={"111": {"url": "x"}},
        download_dir=str(tmp_path),
    )
    target_txt = Path(draft.path)
    target_txt.parent.mkdir(parents=True, exist_ok=True)
    target_txt.write_text(draft.content or "", encoding="utf-8")

    existing_epub = target_txt.with_suffix(".epub")
    sentinel_bytes = b"existing-valid-epub-sentinel"
    existing_epub.write_bytes(sentinel_bytes)

    res = EpubBuilder().create_epub(draft)
    assert res.ok is False
    assert res.written is False
    assert res.embedded_images == 0
    assert res.missing_images == 1
    assert res.output_path == existing_epub
    assert existing_epub.read_bytes() == sentinel_bytes
    assert not list(target_txt.parent.glob(".*.tmp"))
