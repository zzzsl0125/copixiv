"""On-demand multi-chapter EPUB3 and merged TXT builders for folded series."""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from copixiv.core import naming as formats
from copixiv.core.models import UNKNOWN_AUTHOR, EpubStatus
from copixiv.core.text import IMAGE_PLACEHOLDER_PATTERN
from copixiv.log import logger
from copixiv.storage import assets
from copixiv.storage.atomic import atomic_write, resolve_within_root, write_text_atomic
from copixiv.storage.epub.builder import (
    _CONTAINER_XML,
    _XML_ID_SAFE_RE,
    CSS_STYLE,
    EpubBuilder,
    _xml_text,
)

_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[零一二三四五六七八九十百千万兩两\d]+\s*[章话話回卷节節部幕]"
    r"|序章|序言|序(?:\s|$|[：:·、])|楔子|引子|终章|終章|尾声|尾聲|番外|后记|後記"
    r"|(?:chapter|ch\.?|episode|ep\.?)\s*\d+\b"
    r"|(?:prologue|epilogue)\b"
    r")",
    re.IGNORECASE,
)

_MAX_IMAGE_MEMBER_BYTES = 16 * 1024 * 1024
_IMAGE_MEMBER_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})


@dataclass(frozen=True, slots=True)
class SeriesChapterSource:
    """Single chapter file reference used during on-demand series synthesis."""

    novel_id: int
    title: str
    series_index: int | None
    text_path: Path
    epub_path: Path | None = None
    has_epub: int = EpubStatus.NO


@dataclass(frozen=True, slots=True)
class SeriesBookSpec:
    """Series metadata and ordered chapters for on-demand TXT / EPUB3 merging."""

    series_id: int
    title: str
    author_name: str | None
    source: str
    chapters: Sequence[SeriesChapterSource]


def format_chapter_heading(title: str, series_index: int | None, position: int) -> str:
    """Format a chapter heading without double-prefixing existing chapter markers."""
    cleaned = (
        title or ""
    ).strip() or f"第{series_index if series_index and series_index > 0 else position}章"
    if _CHAPTER_HEADING_RE.match(cleaned):
        return cleaned
    chapter_num = series_index if series_index is not None and series_index > 0 else position
    return f"第{chapter_num}章 {cleaned}"


def _resolve_chapter_text_path(path: Path, download_root: Path | None) -> Path | None:
    if download_root is not None:
        resolved = resolve_within_root(path, download_root)
        if resolved is None:
            return None
        path = resolved
    return path if path.is_file() else None


def write_series_txt(
    spec: SeriesBookSpec,
    output_path: Path,
    *,
    download_root: Path | None = None,
) -> int:
    """Synthesize a merged UTF-8 ``.txt`` file for all available chapters in *spec*.

    Returns the number of chapters included. Raises :class:`FileNotFoundError`
    when zero readable chapter files exist on disk.
    """
    resolved_root = download_root.resolve() if download_root is not None else None
    sections: list[str] = []
    included = 0

    for chapter in spec.chapters:
        txt_file = _resolve_chapter_text_path(chapter.text_path, resolved_root)
        if txt_file is None:
            logger.warning("合并 TXT 跳过缺失章节: series={} novel={}", spec.series_id, chapter.novel_id)
            continue
        try:
            body = txt_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("合并 TXT 读取章节失败: series={} path={}", spec.series_id, txt_file)
            continue
        if not body.strip():
            continue

        included += 1
        heading = format_chapter_heading(chapter.title, chapter.series_index, included)
        sections.append(f"{heading}\n\n{body.strip()}")

    if included == 0:
        raise FileNotFoundError(f"系列 {spec.series_id} 没有可用的本地正文文件")

    author_label = (spec.author_name or "").strip() or UNKNOWN_AUTHOR
    header = f"{spec.title or f'系列 #{spec.series_id}'}\n作者：{author_label}\n========\n\n"
    merged_text = header + "\n\n\n".join(sections) + "\n"
    write_text_atomic(output_path, merged_text, root=output_path.parent)
    return included


def _compress_image_bytes(raw_bytes: bytes, quality: int = 75) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(raw_bytes)) as opened:
        img: Image.Image = opened
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def _extract_epub_assets(
    epub_path: Path | None,
    *,
    want_cover: bool,
    quality: int,
) -> tuple[bytes | None, dict[str, bytes]]:
    """Extract embedded cover and body images from a chapter EPUB archive."""
    if epub_path is None or not epub_path.is_file():
        return None, {}

    cover_bytes: bytes | None = None
    images_by_id: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size <= 0 or info.file_size > _MAX_IMAGE_MEMBER_BYTES:
                    continue
                normalized = info.filename.replace("\\", "/")
                lower = normalized.lower()
                ext = Path(lower).suffix
                if ext not in _IMAGE_MEMBER_EXTS:
                    continue

                stem = Path(normalized).stem
                if want_cover and cover_bytes is None and Path(lower).stem == "cover":
                    raw = zf.read(info.filename)
                    cover_bytes = raw if ext in (".jpg", ".jpeg") else _compress_image_bytes(raw, quality)
                    continue

                if "/images/" in f"/{lower}":
                    raw = zf.read(info.filename)
                    images_by_id.setdefault(
                        stem,
                        raw if ext in (".jpg", ".jpeg") else _compress_image_bytes(raw, quality),
                    )
    except Exception:
        logger.debug("读取单章 EPUB 内嵌图片失败，将尝试回退磁盘资产: {}", epub_path)
    return cover_bytes, images_by_id


def _render_chapter_content(
    content: str,
    *,
    chapter_pos: int,
    epub_images: dict[str, bytes],
    text_dir: Path,
    novel_id: int,
    compress_quality: int,
) -> tuple[str, dict[str, bytes]]:
    """Replace chapter image placeholders with series-namespaced ``EPUB/images/...`` tags."""
    embedded: dict[str, bytes] = {}
    disk_map: dict[str, Path] | None = None
    escaped = _xml_text(content)

    def _replace(match: re.Match[str]) -> str:
        nonlocal disk_map
        img_id = match.group(2)
        safe_id = _XML_ID_SAFE_RE.sub("_", img_id)
        key = f"c{chapter_pos:04d}_{safe_id}"
        href = f"images/{key}.jpg"

        if key in embedded:
            return (
                '<div class="illust-container">'
                f'<img src="{href}" alt="Image {html_alt(img_id)}" class="illust" />'
                "</div>"
            )

        if img_id in epub_images:
            embedded[key] = epub_images[img_id]
            return (
                '<div class="illust-container">'
                f'<img src="{href}" alt="Image {html_alt(img_id)}" class="illust" />'
                "</div>"
            )

        if disk_map is None:
            disk_map = assets.scan_assets(text_dir, novel_id)
        img_path = disk_map.get(img_id)
        if img_path is not None and img_path.is_file():
            try:
                data, _media, _ext = EpubBuilder._compress_image(img_path, compress_quality)
                embedded[key] = data
                return (
                    '<div class="illust-container">'
                    f'<img src="{href}" alt="Image {html_alt(img_id)}" class="illust" />'
                    "</div>"
                )
            except Exception:
                logger.exception("压缩系列章节插图失败: {}", img_path)

        return EpubBuilder._missing_image_note(img_id)

    return IMAGE_PLACEHOLDER_PATTERN.sub(_replace, escaped), embedded


def html_alt(img_id: str) -> str:
    return _xml_text(img_id)


def _build_chapter_xhtml(heading: str, processed_body: str) -> bytes:
    html_body = processed_body.replace("\n", "<br />")
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        f"  <title>{_xml_text(heading)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="style/nav.css" />\n'
        "</head>\n"
        "<body>\n"
        f"  <h1>{_xml_text(heading)}</h1>\n"
        "  <hr />\n"
        f"  <div>{html_body}</div>\n"
        "</body>\n"
        "</html>\n"
    )
    return xhtml.encode("utf-8")


def _build_series_opf(
    *,
    series_id: int,
    title: str,
    author_name: str,
    has_cover: bool,
    chapter_files: Sequence[tuple[str, str]],
    image_keys: Sequence[str],
) -> bytes:
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_items = [
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />',
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />',
        '    <item id="style_nav" href="style/nav.css" media-type="text/css" />',
    ]
    spine_items: list[str] = []
    cover_meta = ""
    if has_cover:
        cover_meta = '    <meta name="cover" content="cover-img" />\n'
        manifest_items.append(
            '    <item id="cover-img" href="cover.jpg" media-type="image/jpeg" properties="cover-image" />'
        )
        manifest_items.append(
            '    <item id="cover-page" href="cover_page.xhtml" media-type="application/xhtml+xml" properties="cover" />'
        )
        spine_items.append('    <itemref idref="cover-page" />')

    for item_id, href in chapter_files:
        manifest_items.append(f'    <item id="{item_id}" href="{href}" media-type="application/xhtml+xml" />')
        spine_items.append(f'    <itemref idref="{item_id}" />')

    for idx, img_key in enumerate(image_keys):
        safe_id = _XML_ID_SAFE_RE.sub("_", img_key)
        manifest_items.append(
            f'    <item id="img_{idx}_{safe_id}" href="images/{_xml_text(img_key)}.jpg" media-type="image/jpeg" />'
        )

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">copixiv-series-{series_id}</dc:identifier>\n'
        f"    <dc:title>{_xml_text(title)}</dc:title>\n"
        f"    <dc:creator>{_xml_text(author_name)}</dc:creator>\n"
        "    <dc:language>zh</dc:language>\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        f"{cover_meta}"
        "  </metadata>\n"
        "  <manifest>\n"
        f"{chr(10).join(manifest_items)}\n"
        "  </manifest>\n"
        '  <spine toc="ncx">\n'
        f"{chr(10).join(spine_items)}\n"
        "  </spine>\n"
        "</package>\n"
    )
    return opf.encode("utf-8")


def _build_series_nav(
    title: str,
    *,
    has_cover: bool,
    chapters: Sequence[tuple[str, str]],
) -> bytes:
    entries: list[str] = []
    if has_cover:
        entries.append('      <li><a href="cover_page.xhtml">封面</a></li>')
    for href, heading in chapters:
        entries.append(f'      <li><a href="{href}">{_xml_text(heading)}</a></li>')
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh" lang="zh">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        f"  <title>{_xml_text(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="style/nav.css" />\n'
        "</head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        "    <h1>目录</h1>\n"
        "    <ol>\n"
        f"{chr(10).join(entries)}\n"
        "    </ol>\n"
        "  </nav>\n"
        "</body>\n"
        "</html>\n"
    )
    return nav.encode("utf-8")


def _build_series_ncx(
    series_id: int,
    title: str,
    *,
    has_cover: bool,
    chapters: Sequence[tuple[str, str]],
) -> bytes:
    nav_points: list[str] = []
    order = 1
    if has_cover:
        nav_points.append(
            f'    <navPoint id="navPoint-{order}" playOrder="{order}">\n'
            "      <navLabel><text>封面</text></navLabel>\n"
            '      <content src="cover_page.xhtml" />\n'
            "    </navPoint>"
        )
        order += 1
    for href, heading in chapters:
        nav_points.append(
            f'    <navPoint id="navPoint-{order}" playOrder="{order}">\n'
            f"      <navLabel><text>{_xml_text(heading)}</text></navLabel>\n"
            f'      <content src="{href}" />\n'
            "    </navPoint>"
        )
        order += 1
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="copixiv-series-{series_id}" />\n'
        '    <meta name="dtb:depth" content="1" />\n'
        '    <meta name="dtb:totalPageCount" content="0" />\n'
        '    <meta name="dtb:maxPageNumber" content="0" />\n'
        "  </head>\n"
        f"  <docTitle><text>{_xml_text(title)}</text></docTitle>\n"
        "  <navMap>\n"
        f"{chr(10).join(nav_points)}\n"
        "  </navMap>\n"
        "</ncx>\n"
    )
    return ncx.encode("utf-8")


def write_series_epub(
    spec: SeriesBookSpec,
    output_path: Path,
    *,
    download_root: Path | None = None,
    compress_quality: int = 75,
) -> int:
    """Synthesize a multi-chapter EPUB3 archive for *spec* with TOC and images.

    Returns the number of chapters included. Raises :class:`FileNotFoundError`
    when zero readable chapter files exist on disk.
    """
    resolved_root = download_root.resolve() if download_root is not None else None
    cover_bytes: bytes | None = None
    embedded_images: dict[str, bytes] = {}
    chapter_entries: list[tuple[str, str, str, bytes]] = []  # (item_id, href, heading, xhtml_bytes)

    for chapter in spec.chapters:
        txt_file = _resolve_chapter_text_path(chapter.text_path, resolved_root)
        if txt_file is None:
            logger.warning("合并 EPUB 跳过缺失章节: series={} novel={}", spec.series_id, chapter.novel_id)
            continue
        try:
            body = txt_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("合并 EPUB 读取章节失败: series={} path={}", spec.series_id, txt_file)
            continue
        if not body.strip():
            continue

        sibling_epub = chapter.epub_path
        if sibling_epub is None:
            candidate_epub = formats.sibling_path(txt_file, formats.EPUB)
            if candidate_epub.is_file():
                sibling_epub = candidate_epub
        elif resolved_root is not None:
            sibling_epub = resolve_within_root(sibling_epub, resolved_root)

        ch_cover, epub_imgs = _extract_epub_assets(
            sibling_epub,
            want_cover=(cover_bytes is None),
            quality=compress_quality,
        )
        if cover_bytes is None:
            if ch_cover is not None:
                cover_bytes = ch_cover
            else:
                disk_cover = assets.find_cover(txt_file.parent, chapter.novel_id)
                cover_bytes = EpubBuilder._prepare_cover(disk_cover, compress_quality)

        processed_body, chapter_imgs = _render_chapter_content(
            body,
            chapter_pos=len(chapter_entries) + 1,
            epub_images=epub_imgs,
            text_dir=txt_file.parent,
            novel_id=chapter.novel_id,
            compress_quality=compress_quality,
        )
        embedded_images.update(chapter_imgs)

        ch_num = len(chapter_entries) + 1
        heading = format_chapter_heading(chapter.title, chapter.series_index, ch_num)
        item_id = f"ch_{ch_num:04d}"
        href = f"{item_id}.xhtml"
        xhtml_bytes = _build_chapter_xhtml(heading, processed_body)
        chapter_entries.append((item_id, href, heading, xhtml_bytes))

    if not chapter_entries:
        raise FileNotFoundError(f"系列 {spec.series_id} 没有可用的本地正文文件")

    book_title = (spec.title or "").strip() or f"系列 #{spec.series_id}"
    author_name = (spec.author_name or "").strip() or UNKNOWN_AUTHOR
    has_cover = cover_bytes is not None

    chapter_manifest = [(item_id, href) for item_id, href, _, _ in chapter_entries]
    chapter_toc = [(href, heading) for _, href, heading, _ in chapter_entries]

    opf_bytes = _build_series_opf(
        series_id=spec.series_id,
        title=book_title,
        author_name=author_name,
        has_cover=has_cover,
        chapter_files=chapter_manifest,
        image_keys=list(embedded_images),
    )
    nav_bytes = _build_series_nav(book_title, has_cover=has_cover, chapters=chapter_toc)
    ncx_bytes = _build_series_ncx(spec.series_id, book_title, has_cover=has_cover, chapters=chapter_toc)
    cover_page_bytes = EpubBuilder._build_cover_xhtml() if has_cover else None

    with atomic_write(output_path, root=output_path.parent) as tmp_path, zipfile.ZipFile(tmp_path, "w") as zf:
        zf.writestr(
            zipfile.ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/container.xml", _CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/content.opf", opf_bytes, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/nav.xhtml", nav_bytes, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/toc.ncx", ncx_bytes, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/style/nav.css", CSS_STYLE.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        if cover_bytes is not None and cover_page_bytes is not None:
            zf.writestr("EPUB/cover.jpg", cover_bytes, compress_type=zipfile.ZIP_STORED)
            zf.writestr("EPUB/cover_page.xhtml", cover_page_bytes, compress_type=zipfile.ZIP_DEFLATED)
        for img_key, img_bytes in embedded_images.items():
            zf.writestr(f"EPUB/images/{img_key}.jpg", img_bytes, compress_type=zipfile.ZIP_STORED)
        for _, href, _, xhtml_bytes in chapter_entries:
            zf.writestr(f"EPUB/{href}", xhtml_bytes, compress_type=zipfile.ZIP_DEFLATED)

    return len(chapter_entries)


__all__ = [
    "SeriesBookSpec",
    "SeriesChapterSource",
    "format_chapter_heading",
    "write_series_epub",
    "write_series_txt",
]
