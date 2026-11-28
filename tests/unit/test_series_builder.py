"""Unit tests for on-demand series TXT/EPUB3 synthesis and batch ZIP merging."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from copixiv.core.models import EpubStatus
from copixiv.storage.epub.series_builder import (
    SeriesBookSpec,
    SeriesChapterSource,
    format_chapter_heading,
    write_series_epub,
    write_series_txt,
)
from copixiv.storage.zip import SeriesExportEntry, build_batch_zip_to_file, zip_entry_path


def _write_png(path: Path, color: tuple[int, int, int] = (20, 120, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color).save(path, format="PNG")


def _make_chapter_epub(
    path: Path,
    *,
    cover_bytes: bytes | None = None,
    images: dict[str, bytes] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip")
        if cover_bytes is not None:
            zf.writestr("EPUB/cover.jpg", cover_bytes)
        for name, data in (images or {}).items():
            zf.writestr(f"EPUB/images/{name}.jpg", data)


def test_format_chapter_heading_deduplicates_existing_chapter_prefixes():
    assert format_chapter_heading("第一章 初遇", 1, 1) == "第一章 初遇"
    assert format_chapter_heading("Chapter 2: Awakening", 2, 2) == "Chapter 2: Awakening"
    assert format_chapter_heading("序章 风起", 1, 1) == "序章 风起"
    assert format_chapter_heading("番外 日常", None, 4) == "番外 日常"
    assert format_chapter_heading("森林之夜", 3, 2) == "第3章 森林之夜"
    assert format_chapter_heading("后日谈", None, 5) == "第5章 后日谈"
    assert format_chapter_heading("", None, 2) == "第2章"


def test_write_series_txt_merges_available_chapters_and_handles_missing(tmp_path: Path):
    root = tmp_path / "dl"
    series_dir = root / "pixiv" / "长篇系列_100"
    series_dir.mkdir(parents=True)
    ch1 = series_dir / "0001_序章_101.txt"
    ch2 = series_dir / "0002_出发_102.txt"
    missing_ch = series_dir / "0003_丢失_103.txt"
    ch1.write_text("第一段正文。", encoding="utf-8")
    ch2.write_text("第二段正文。", encoding="utf-8")

    spec = SeriesBookSpec(
        series_id=100,
        title="长篇系列",
        author_name="作者甲",
        source="pixiv",
        chapters=[
            SeriesChapterSource(101, "序章", 1, ch1),
            SeriesChapterSource(103, "丢失章", 3, missing_ch),
            SeriesChapterSource(102, "出发", 2, ch2),
        ],
    )
    out = tmp_path / "merged.txt"
    included = write_series_txt(spec, out, download_root=root)
    assert included == 2
    content = out.read_text(encoding="utf-8")
    assert "长篇系列\n作者：作者甲" in content
    assert "序章\n\n第一段正文。" in content
    assert "第2章 出发\n\n第二段正文。" in content
    assert "丢失章" not in content

    empty_spec = SeriesBookSpec(
        series_id=100,
        title="空书",
        author_name=None,
        source="pixiv",
        chapters=[SeriesChapterSource(103, "丢失章", 3, missing_ch)],
    )
    with pytest.raises(FileNotFoundError):
        write_series_txt(empty_spec, tmp_path / "empty.txt", download_root=root)


def test_write_series_epub_packages_toc_cover_and_namespaced_images(tmp_path: Path):
    root = tmp_path / "dl"
    book_dir = root / "esj" / "冒險譚_50"
    book_dir.mkdir(parents=True)

    ch1_txt = book_dir / "0001_第一章 啟程_-1.txt"
    ch1_epub = book_dir / "0001_第一章 啟程_-1.epub"
    ch2_txt = book_dir / "0002_重逢_-2.txt"
    ch1_txt.write_text("前文\n[uploadedimage:10]\n[pixivimage:99]\n后文", encoding="utf-8")
    ch2_txt.write_text("次章正文\n[uploadedimage:10]", encoding="utf-8")

    # Disk fallback image & cover image
    cover_png = tmp_path / "tmp_cover.png"
    img10_png = tmp_path / "tmp_img10.png"
    _write_png(cover_png, (200, 10, 10))
    _write_png(img10_png, (10, 200, 10))
    _make_chapter_epub(
        ch1_epub,
        cover_bytes=cover_png.read_bytes(),
        images={"10": img10_png.read_bytes()},
    )
    # Disk fallback image for ch2 (same image key '10' must be namespaced c0002_10 without colliding!)
    _write_png(book_dir / "-2_u_10.png", (10, 10, 200))

    spec = SeriesBookSpec(
        series_id=-50,
        title="冒險譚",
        author_name="譯者A",
        source="esj",
        chapters=[
            SeriesChapterSource(-1, "第一章 啟程", 1, ch1_txt, epub_path=ch1_epub, has_epub=EpubStatus.DONE),
            SeriesChapterSource(-2, "重逢", 2, ch2_txt, has_epub=EpubStatus.PENDING),
        ],
    )
    out_epub = tmp_path / "series.epub"
    included = write_series_epub(spec, out_epub, download_root=root)
    assert included == 2

    with zipfile.ZipFile(out_epub) as zf:
        infos = zf.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        names = set(zf.namelist())
        assert {
            "META-INF/container.xml",
            "EPUB/content.opf",
            "EPUB/nav.xhtml",
            "EPUB/toc.ncx",
            "EPUB/style/nav.css",
            "EPUB/cover.jpg",
            "EPUB/cover_page.xhtml",
            "EPUB/ch_0001.xhtml",
            "EPUB/ch_0002.xhtml",
            "EPUB/images/c0001_10.jpg",
            "EPUB/images/c0002_10.jpg",
        } <= names

        nav = zf.read("EPUB/nav.xhtml").decode("utf-8")
        ncx = zf.read("EPUB/toc.ncx").decode("utf-8")
        opf = zf.read("EPUB/content.opf").decode("utf-8")
        ch1_html = zf.read("EPUB/ch_0001.xhtml").decode("utf-8")
        ch2_html = zf.read("EPUB/ch_0002.xhtml").decode("utf-8")

        assert "第一章 啟程" in nav
        assert "第2章 重逢" in nav
        assert "第1章 第一章 啟程" not in nav
        assert "第2章 重逢" in ncx
        assert "冒險譚" in opf
        assert "譯者A" in opf
        assert 'src="images/c0001_10.jpg"' in ch1_html
        assert "illust-missing" in ch1_html  # missing pixivimage:99 rendered safely
        assert 'src="images/c0002_10.jpg"' in ch2_html


def test_build_batch_zip_with_mixed_standalone_and_series_entries(tmp_path: Path):
    root = tmp_path / "dl"
    solo_path = root / "local" / "短篇_-5.txt"
    solo_path.parent.mkdir(parents=True)
    solo_path.write_text("独立短篇内容", encoding="utf-8")

    sdir = root / "pixiv" / "合集_10"
    sdir.mkdir(parents=True)
    c1 = sdir / "0001_C1_101.txt"
    c2 = sdir / "0002_C2_102.txt"
    c1.write_text("C1内容", encoding="utf-8")
    c2.write_text("C2内容", encoding="utf-8")

    solo = SimpleNamespace(
        id=-5,
        title="短篇",
        author_id=None,
        author_name="本地作者",
        series_name=None,
        series_index=None,
        like=1,
        view=2,
        text=6,
        create_time=None,
        has_epub=EpubStatus.NO_IMAGES,
        source="local",
        path=str(solo_path),
    )
    series_entry = SeriesExportEntry(
        spec=SeriesBookSpec(
            series_id=10,
            title="合集",
            author_name="作者B",
            source="pixiv",
            chapters=[
                SeriesChapterSource(101, "C1", 1, c1),
                SeriesChapterSource(102, "C2", 2, c2),
            ],
        ),
        id=102,
        title="合集",
        author_id=2,
        author_name="作者B",
        has_epub=EpubStatus.DONE,
        source="pixiv",
    )
    # series_name & series_index cleared so default naming template avoids nested book folder
    assert zip_entry_path(series_entry, "prefer_epub") == "作者B/合集_102.epub"

    out_zip = tmp_path / "mixed.zip"
    size_bytes, added, missing = build_batch_zip_to_file(
        [solo, series_entry],
        out_zip,
        format_mode="prefer_epub",
        download_root=root,
    )
    assert size_bytes > 0
    assert added == 2
    assert missing == []
    with zipfile.ZipFile(out_zip) as zf:
        names = set(zf.namelist())
        assert "本地作者/短篇_-5.txt" in names
        assert "作者B/合集_102.epub" in names
        with zipfile.ZipFile(zf.open("作者B/合集_102.epub")) as inner:
            assert "EPUB/ch_0001.xhtml" in inner.namelist()
            assert "EPUB/ch_0002.xhtml" in inner.namelist()
