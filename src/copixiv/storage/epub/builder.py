"""EPUB3 builder — standard-library ``zipfile`` packaging with structured results."""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from copixiv.core import naming as formats
from copixiv.core.draft import NovelDraft
from copixiv.core.text import IMAGE_PLACEHOLDER_PATTERN
from copixiv.log import logger
from copixiv.storage import assets
from copixiv.storage.atomic import atomic_write

CSS_STYLE = """
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 5%; text-align: justify; }
h1 { text-align: center; }
.author { text-align: center; font-style: italic; margin-bottom: 2em; }
.illust-container { text-align: center; margin: 1em 0; }
.illust { max-width: 100%; height: auto; }
.illust-missing { border: 1px dashed #999; color: #777; padding: 1.5em; font-size: 0.9em; }
.cover-container { text-align: center; height: 100%; display: flex; justify-content: center; align-items: center; }
.cover-image { max-width: 100%; max-height: 100%; object-fit: contain; }
"""

# Control chars disallowed in XML 1.0 (everything below 0x20 except \t \n \r).
_INVALID_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_CONTAINER_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<container version="1.0" '
    b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    b"  <rootfiles>\n"
    b'    <rootfile full-path="EPUB/content.opf" '
    b'media-type="application/oebps-package+xml"/>\n'
    b"  </rootfiles>\n"
    b"</container>\n"
)


@dataclass(frozen=True)
class EpubBuildResult:
    """Structured outcome from :meth:`EpubBuilder.create_epub`.

    Carrying image embedding and raw-marker counts here spares callers from
    re-opening the written ZIP just to verify completeness.
    """

    ok: bool
    written: bool = False
    output_path: Path | None = None
    embedded_images: int = 0
    missing_images: int = 0
    has_raw_markers: bool = False
    has_cover: bool = False

    @property
    def is_complete(self) -> bool:
        """True when a built EPUB embedded ≥1 illustration and left no raw markers."""
        return self.ok and self.written and self.embedded_images > 0 and not self.has_raw_markers

    def __bool__(self) -> bool:
        return self.ok


def _xml_text(text: str) -> str:
    """Strip invalid XML 1.0 control characters and escape XML entities."""
    return html.escape(_INVALID_XML_CHARS_RE.sub("", text), quote=True)


class EpubBuilder:
    """Creates EPUB3 files from downloaded novel text and images."""

    def create_epub(
        self,
        novel: NovelDraft,
        compress_quality: int = 75,
        needs_epub: bool | None = None,
    ) -> EpubBuildResult:
        """Build an EPUB3 archive from *novel*.

        When *needs_epub* is ``False``, skips writing and returns
        ``EpubBuildResult(ok=True, written=False)``.
        """
        if needs_epub is False:
            return EpubBuildResult(ok=True, written=False)

        path_str = novel.path
        if not path_str:
            logger.error("No path provided in novel for EPUB creation")
            return EpubBuildResult(ok=False)

        novel_path = Path(path_str)
        if not novel_path.exists():
            logger.error(f"Source text file not found: {novel_path}")
            return EpubBuildResult(ok=False)

        title = novel.title or "Untitled"
        author_name = novel.author_name or str(novel.author_id or "Unknown Author")
        novel_id = str(novel.id)
        parent_dir = novel_path.parent

        try:
            content = novel_path.read_text(encoding="utf-8")
        except Exception:
            logger.exception(f"Failed to read novel text: {novel_path}")
            return EpubBuildResult(ok=False)

        if not content.strip():
            logger.warning(f"Empty content for {novel_path}, skipping EPUB.")
            return EpubBuildResult(ok=False)

        cover_path = self._find_cover(parent_dir, novel_id)
        cover_bytes = self._prepare_cover(cover_path, compress_quality)

        image_map = self._build_image_map(parent_dir, novel_id, novel)
        processed_content, embedded_images, missing_images = self._replace_image_placeholders(
            content, image_map, compress_quality
        )

        output_path = formats.sibling_path(novel_path, formats.EPUB)
        has_raw_markers = bool(IMAGE_PLACEHOLDER_PATTERN.search(processed_content))
        if has_raw_markers:
            logger.error(f"EPUB 仍含未替换的图片占位符: {output_path.name}")

        has_cover = cover_bytes is not None
        opf_bytes = self._build_opf(
            novel_id=novel_id,
            title=title,
            author_name=author_name,
            has_cover=has_cover,
            image_ids=list(embedded_images),
        )
        nav_bytes = self._build_nav(title, has_cover=has_cover)
        ncx_bytes = self._build_ncx(novel_id, title, has_cover=has_cover)
        content_bytes = self._build_main_xhtml(title, author_name, processed_content)
        cover_page_bytes = self._build_cover_xhtml() if has_cover else None

        try:
            with atomic_write(output_path) as tmp_path, zipfile.ZipFile(tmp_path, "w") as zf:
                # OCF spec requires ``mimetype`` as the first, uncompressed entry.
                zf.writestr(
                    zipfile.ZipInfo("mimetype"),
                    b"application/epub+zip",
                    compress_type=zipfile.ZIP_STORED,
                )
                zf.writestr(
                    "META-INF/container.xml",
                    _CONTAINER_XML,
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                zf.writestr(
                    "EPUB/content.opf",
                    opf_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                zf.writestr(
                    "EPUB/nav.xhtml",
                    nav_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                zf.writestr(
                    "EPUB/toc.ncx",
                    ncx_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                zf.writestr(
                    "EPUB/style/nav.css",
                    CSS_STYLE.encode("utf-8"),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                if cover_bytes is not None and cover_page_bytes is not None:
                    zf.writestr(
                        "EPUB/cover.jpg",
                        cover_bytes,
                        compress_type=zipfile.ZIP_STORED,
                    )
                    zf.writestr(
                        "EPUB/cover_page.xhtml",
                        cover_page_bytes,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
                for img_id, img_bytes in embedded_images.items():
                    zf.writestr(
                        f"EPUB/images/{img_id}.jpg",
                        img_bytes,
                        compress_type=zipfile.ZIP_STORED,
                    )
                zf.writestr(
                    "EPUB/content.xhtml",
                    content_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                )
            logger.info(f"Made Epub: ({novel.id}){novel.title}")
            return EpubBuildResult(
                ok=True,
                written=True,
                output_path=output_path,
                embedded_images=len(embedded_images),
                missing_images=missing_images,
                has_raw_markers=has_raw_markers,
                has_cover=has_cover,
            )
        except Exception:
            logger.exception(f"Failed to write EPUB: {output_path}")
            return EpubBuildResult(ok=False)

    # ------------------------------------------------------------------
    # Image helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compress_image(image_path: Path, quality: int = 75) -> tuple[bytes, str, str]:
        from PIL import Image

        with Image.open(image_path) as opened:
            img: Image.Image = opened
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            return buffer.getvalue(), "image/jpeg", ".jpg"

    @staticmethod
    def _prepare_cover(cover_path: Path | None, quality: int = 75) -> bytes | None:
        if not cover_path or not cover_path.exists():
            return None
        try:
            data, _media_type, _ext = EpubBuilder._compress_image(cover_path, quality)
            return data
        except Exception:
            logger.exception(f"Failed to set cover: {cover_path}")
            return None

    @staticmethod
    def _replace_image_placeholders(
        content: str,
        image_map: dict[str, Path],
        quality: int = 75,
    ) -> tuple[str, dict[str, bytes], int]:
        """Replace ``[uploadedimage:id]`` / ``[pixivimage:id]`` markers with XHTML."""
        embedded: dict[str, bytes] = {}
        missing_count = 0
        escaped_content = _xml_text(content)

        def _replace(match: re.Match[str]) -> str:
            nonlocal missing_count
            img_id = match.group(2)
            if img_id in embedded:
                return (
                    '<div class="illust-container">'
                    f'<img src="images/{img_id}.jpg" alt="Image {img_id}"'
                    ' class="illust" />'
                    "</div>"
                )
            if (img_path := image_map.get(img_id)) and img_path.exists():
                try:
                    data, _media, _ext = EpubBuilder._compress_image(img_path, quality)
                    embedded[img_id] = data
                    return (
                        '<div class="illust-container">'
                        f'<img src="images/{img_id}.jpg" alt="Image {img_id}"'
                        ' class="illust" />'
                        "</div>"
                    )
                except Exception:
                    logger.exception(f"Failed to process image {img_path.name}")
            missing_count += 1
            logger.warning(f"EPUB 图片缺失: [{match.group(1)}:{img_id}] 未下载成功 → 渲染为缺图占位框")
            return EpubBuilder._missing_image_note(img_id)

        replaced = IMAGE_PLACEHOLDER_PATTERN.sub(_replace, escaped_content)
        return replaced, embedded, missing_count

    @staticmethod
    def _missing_image_note(img_id: str) -> str:
        """Visible placeholder for an image that could not be embedded."""
        return (
            '<div class="illust-container">'
            f'<div class="illust-missing">（图片缺失 {_xml_text(img_id)}）</div>'
            "</div>"
        )

    @staticmethod
    def _build_image_map(parent_dir: Path, novel_id: str, novel: NovelDraft) -> dict[str, Path]:
        """``{占位符 id: 盘上文件}`` —— 名字约定归 :mod:`copixiv.storage.assets`."""
        known: list[tuple[str, str]] = []
        if isinstance(novel.images, dict):
            known.extend((assets.UPLOADED, k) for k in novel.images)
        if isinstance(novel.illusts, dict):
            known.extend((assets.ILLUSTRATION, k) for k in novel.illusts)

        image_map = assets.iter_assets(
            parent_dir,
            novel_id,
            [(kind, aid) for kind, aid in known],
        )
        return image_map or assets.scan_assets(parent_dir, novel_id)

    @staticmethod
    def _find_cover(parent_dir: Path, novel_id: str) -> Path | None:
        return assets.find_cover(parent_dir, novel_id)

    # ------------------------------------------------------------------
    # EPUB3 package & XHTML templates
    # ------------------------------------------------------------------

    @staticmethod
    def _build_opf(
        *,
        novel_id: str,
        title: str,
        author_name: str,
        has_cover: bool,
        image_ids: list[str],
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
                '    <item id="cover-img" href="cover.jpg" '
                'media-type="image/jpeg" properties="cover-image" />'
            )
            manifest_items.append(
                '    <item id="cover-page" href="cover_page.xhtml" '
                'media-type="application/xhtml+xml" properties="cover" />'
            )
            spine_items.append('    <itemref idref="cover-page" />')

        manifest_items.append(
            '    <item id="content" href="content.xhtml" media-type="application/xhtml+xml" />'
        )
        spine_items.append('    <itemref idref="content" />')

        for idx, img_id in enumerate(image_ids):
            safe_id = _XML_ID_SAFE_RE.sub("_", img_id)
            manifest_items.append(
                f'    <item id="img_{idx}_{safe_id}" href="images/{_xml_text(img_id)}.jpg" '
                'media-type="image/jpeg" />'
            )

        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" '
            'unique-identifier="bookid" version="3.0">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="bookid">copixiv-{_xml_text(novel_id)}</dc:identifier>\n'
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

    @staticmethod
    def _build_nav(title: str, *, has_cover: bool) -> bytes:
        entries: list[str] = []
        if has_cover:
            entries.append('      <li><a href="cover_page.xhtml">封面</a></li>')
        entries.append('      <li><a href="content.xhtml">正文</a></li>')
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

    @staticmethod
    def _build_ncx(novel_id: str, title: str, *, has_cover: bool) -> bytes:
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
        nav_points.append(
            f'    <navPoint id="navPoint-{order}" playOrder="{order}">\n'
            "      <navLabel><text>正文</text></navLabel>\n"
            '      <content src="content.xhtml" />\n'
            "    </navPoint>"
        )
        ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            "  <head>\n"
            f'    <meta name="dtb:uid" content="copixiv-{_xml_text(novel_id)}" />\n'
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

    @staticmethod
    def _build_cover_xhtml() -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<!DOCTYPE html>\n"
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">\n'
            "<head>\n"
            '  <meta charset="utf-8" />\n'
            "  <title>封面</title>\n"
            '  <link rel="stylesheet" type="text/css" href="style/nav.css" />\n'
            "</head>\n"
            "<body>\n"
            '  <div class="cover-container">\n'
            '    <img src="cover.jpg" alt="Cover" class="cover-image" />\n'
            "  </div>\n"
            "</body>\n"
            "</html>\n"
        ).encode()

    @staticmethod
    def _build_main_xhtml(title: str, author_name: str, processed_content: str) -> bytes:
        html_body = processed_content.replace("\n", "<br />")
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<!DOCTYPE html>\n"
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">\n'
            "<head>\n"
            '  <meta charset="utf-8" />\n'
            f"  <title>{_xml_text(title)}</title>\n"
            '  <link rel="stylesheet" type="text/css" href="style/nav.css" />\n'
            "</head>\n"
            "<body>\n"
            f"  <h1>{_xml_text(title)}</h1>\n"
            f'  <p class="author">Author: {_xml_text(author_name)}</p>\n'
            "  <hr />\n"
            f"  <div>{html_body}</div>\n"
            "</body>\n"
            "</html>\n"
        )
        return xhtml.encode("utf-8")


__all__ = ["CSS_STYLE", "EpubBuildResult", "EpubBuilder"]
