"""EPUB builder — converts downloaded novel text + images into an EPUB file."""

import html
import io
import os
import re
import zipfile
from pathlib import Path

from PIL import Image
from ebooklib import epub

from copixiv.core.draft import NovelDraft
from copixiv.core.services import build_path, has_image_placeholders

from copixiv.log import logger

# Pattern for embedded image placeholders — keep in sync with domain.services.language
_HAS_IMAGE_PATTERN = re.compile(
    r"\[(uploadedimage|pixivimage):([\d\-]+)\]"
)


def _fit_basename(path: Path, suffix: str) -> Path:
    """``path`` with *suffix* swapped in, truncated to fit NAME_MAX.

    Truncation happens on the **stem only**, in *path*'s own directory: a
    caller may have built the path from a raw title (251-253 bytes, where the
    transient ``.tmp`` write fails with Errno 36), and the fix must not move
    the file — re-deriving it from ``download_dir`` gives a *relative* path
    and would drop the EPUB outside the caller's tree entirely.

    Idempotent: a basename that already fits is returned unchanged, which is
    the case for every path that came from ``build_path``.
    """
    candidate = path.with_suffix(suffix)
    raw = candidate.name.encode("utf-8")
    if len(raw) <= _NAME_MAX_BUDGET:
        return candidate
    budget = _NAME_MAX_BUDGET - len(suffix.encode("utf-8"))
    stem = path.stem.encode("utf-8")[:budget]
    while True:
        try:
            stem_str = stem.decode("utf-8")     # never split a character
            break
        except UnicodeDecodeError:
            stem = stem[:-1]
    return path.with_name(f"{stem_str}{suffix}")


def resolve_epub_path(txt_path: Path) -> Path:
    """Where this novel's EPUB lives — same stem, else by ``_{id}`` suffix.

    Truncation can make the EPUB's basename differ from the text file's (a
    251-253 byte name loses its last characters so the ".tmp" write fits),
    while every reader — ``check_epub``, the download API, the static mount —
    derives the EPUB from the *stored text path*.  Falling back to a sibling
    whose name ends in ``_{novel_id}.epub`` keeps such a novel findable
    instead of looking like a missing file that gets queued and rebuilt on
    every sweep.
    """
    direct = txt_path.with_suffix(".epub")
    if direct.is_file():
        return direct
    stem = txt_path.stem
    if "_" not in stem:
        return direct
    novel_id = stem.rsplit("_", 1)[1]
    if not novel_id.isdigit():
        return direct

    # The truncated name shares a long prefix with the text stem (the tail is
    # what got cut).  Longest common prefix first: it cannot collide with a
    # neighbouring novel because the id digits are the last thing before the
    # suffix.
    for cut in range(len(stem), max(len(stem) - 40, 0), -1):
        prefix = stem[:cut]
        if len(prefix) < 8:
            break
        for candidate in sorted(txt_path.parent.glob(f"{prefix}*.epub")):
            if candidate.is_file():
                return candidate

    # Fallback: an "_{id…}" fragment, since truncation can cut into the id
    # itself ("…_275491.epub" for id 27549104).
    for length in range(len(novel_id), 3, -1):
        for candidate in sorted(
            txt_path.parent.glob(f"*_{novel_id[:length]}*.epub")
        ):
            if candidate.is_file():
                return candidate
    return direct


def is_valid_epub(epub_path: Path) -> bool:
    """True when *epub_path* exists and is a readable zip container.

    Deliberately weak: this answers "can it be opened as an EPUB at all",
    not "is it complete".  A zip whose XHTML still holds raw image
    placeholders passes here — use :func:`is_readable_epub` semantics plus a
    content check (``check_epub`` does) before calling a novel done.
    """
    return epub_path.is_file() and zipfile.is_zipfile(epub_path)

# Filesystem basename limit (bytes).  The write goes through a ".tmp"
# sibling first, and ``build_path`` reserves that suffix; this constant is
# the budget an already-built path must respect to be used as-is.
_NAME_MAX_BUDGET = 250

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


class EpubBuilder:
    """Creates EPUB files from downloaded novel text and images."""

    def create_epub(
        self,
        novel: NovelDraft,
        compress_quality: int = 75,
        needs_epub: bool | None = None,
    ) -> bool:
        """Build an EPUB from the write-path *novel* draft.

        Typed input (docs/MODULARITY.md §M5): the builder consumes the
        write-path :class:`~copixiv.core.draft.NovelDraft`,
        never a raw dict.

        *needs_epub* lets the caller（the asset downloader, which holds the
        API body）state whether the text actually contains image
        placeholders: ``False`` skips the build entirely so a novel without
        images never gains an empty-shell EPUB.  ``None``（default）means
        "caller did not judge" — build unconditionally, which is what the
        repair scripts and the regression tests rely on.

        Returns True if the EPUB was written successfully.  A marker whose
        image is missing is no longer a silent success: it degrades to a
        visible 缺图 box (see :meth:`_replace_image_placeholders`).
        """
        if needs_epub is False:
            return True

        path_str = novel.path
        if not path_str:
            logger.error("No path provided in novel for EPUB creation")
            return False

        novel_path = Path(path_str)
        if not novel_path.exists():
            logger.error(f"Source text file not found: {novel_path}")
            return False

        title = novel.title or "Untitled"
        author_name = novel.author_name or str(
            novel.author_id or "Unknown Author"
        )
        novel_id = str(novel.id)
        parent_dir = novel_path.parent

        # Read text
        try:
            content = novel_path.read_text(encoding="utf-8")
        except Exception:
            logger.exception(f"Failed to read novel text: {novel_path}")
            return False

        if not content.strip():
            logger.warning(f"Empty content for {novel_path}, skipping EPUB.")
            return False

        # Build EPUB
        book = epub.EpubBook()
        book.set_identifier(novel_id)
        book.set_title(title)
        book.set_language("zh")
        book.add_author(author_name)

        # Cover
        cover_path = self._find_cover(parent_dir, novel_id)
        self._set_cover(book, cover_path)

        # Images
        image_map = self._build_image_map(parent_dir, novel_id, novel)
        processed_content = self._replace_image_placeholders(
            content, image_map, book, compress_quality
        )

        # Output path: the text file's sibling.  The basename normally needs
        # no work — ``build_path`` already budgeted it against NAME_MAX
        # *including* the transient ".tmp" suffix, and it is the name the
        # database stores, so keeping it is what makes the EPUB findable.
        # A caller that assembled the path from a raw title instead (the
        # repair scripts do) can sit at 251-253 bytes, where the ".tmp" write
        # — created before ``os.replace`` — dies with Errno 36 and no EPUB is
        # produced at all (3 novels, 2026-09-16).  That case is fixed by
        # truncating the *basename* right here, in the caller's own directory
        # (never by re-deriving a path from ``download_dir``, which is
        # relative and would move the file somewhere else entirely).
        output_path = _fit_basename(novel_path, ".epub")
        tmp_path = output_path.with_name(output_path.name + ".tmp")
        if len(tmp_path.name.encode("utf-8")) > _NAME_MAX_BUDGET:
            # Written straight to its final name (no ".tmp" step) — the
            # atomic swap needs two spare bytes the name does not have.
            output_path = _fit_basename(novel_path, ".epub")
            tmp_path = output_path
            logger.warning(f"EPUB 文件名贴顶，改为直接写: {output_path.name[-60:]}")

        # Main page
        main_page = self._build_main_page(title, author_name, processed_content)
        book.add_item(main_page)

        # TOC & nav
        book.toc = [main_page]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # CSS
        nav_css = epub.EpubItem(
            uid="style_nav",
            file_name="style/nav.css",
            media_type="text/css",
            content=CSS_STYLE,
        )
        book.add_item(nav_css)

        # Spine
        spine: list = ["nav"]
        if cover_path and cover_path.exists():
            cover_page = self._build_cover_page()
            book.add_item(cover_page)
            spine.append(cover_page)
            book.toc.insert(0, cover_page)
        spine.append(main_page)
        book.spine = spine

        # Write — atomic: build into a sibling temp file, then os.replace so
        # a crash never leaves a truncated EPUB at the final path.
        # ``output_path``/``tmp_path`` were fitted to NAME_MAX above.
        try:
            epub.write_epub(tmp_path, book, {})
            os.replace(tmp_path, output_path)
            # Belt-and-braces: no internal marker may ever reach the reader.
            # ``_replace_image_placeholders`` already degrades unresolved
            # markers, so this only fires if a future code path bypasses it.
            if _HAS_IMAGE_PATTERN.search(processed_content):
                logger.error(
                    f"EPUB 仍含未替换的图片占位符: {output_path.name}"
                )
            logger.info(f"Made Epub: ({novel.id}){novel.title}")
            return True
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            logger.exception(f"Failed to write EPUB: {output_path}")
            return False

    # ------------------------------------------------------------------
    # Image helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compress_image(
        image_path: Path, quality: int = 75
    ) -> tuple[bytes, str, str]:
        with Image.open(image_path) as img:
            fmt = img.format.lower() if img.format else "jpeg"

            if (
                img.mode in ("RGBA", "LA")
                or (img.mode == "P" and "transparency" in img.info)
            ):
                # Convert to RGBA first and flatten there.  Using
                # ``img.split()[-1]`` as the mask looks equivalent but is not:
                # for a palette image whose transparency is a *colour index*
                # PIL hands back a mask whose size/extent does not match, and
                # ``paste`` dies with ``ValueError: bad transparency mask`` —
                # which silently cost a real illustration on 4 novels
                # (2026-09-16).  convert("RGBA") resolves the palette and the
                # transparency table into a proper alpha channel.
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background
            elif img.mode == "P":
                # Palette without a transparency table: convert() alone keeps
                # the palette (and PIL then refuses to save it as JPEG).
                img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            return buffer.getvalue(), "image/jpeg", ".jpg"

    @staticmethod
    def _add_image_to_epub(
        image_path: Path,
        image_id: str,
        book: epub.EpubBook,
        quality: int = 75,
    ) -> bool:
        if not image_path.exists():
            return False
        try:
            data, media_type, ext = EpubBuilder._compress_image(image_path, quality)
            epub_image = epub.EpubImage()
            epub_image.file_name = f"images/{image_id}{ext}"
            epub_image.media_type = media_type
            epub_image.content = data

            if book.get_item_with_href(epub_image.file_name):
                return True

            book.add_item(epub_image)
            return True
        except Exception:
            logger.exception(f"Failed to process image {image_path.name}")
            return False

    @staticmethod
    def _replace_image_placeholders(
        content: str,
        image_map: dict[str, Path],
        book: epub.EpubBook,
        quality: int = 75,
    ) -> str:
        """Turn every ``[uploadedimage:id]`` marker into an ``<img>`` or a note.

        A marker whose image never reached the disk (download failed, or the
        API response did not carry it) must NOT survive as raw
        ``[uploadedimage:12345]`` text: that shipped silently as a
        supposedly successful EPUB for 216 novels (2026-09 排查).  Such
        markers degrade to a visible dashed placeholder box instead — the
        reader sees an image was expected and is missing, and no internal
        marker ever leaks into the book.
        """
        processed: set[str] = set()

        # Escape the raw novel text before embedding it in XHTML — it may
        # contain <, >, & from the source content.  Image placeholders
        # ([uploadedimage:12345]) contain no HTML-special characters, so
        # they survive escape unchanged and still match below.
        content = html.escape(content)

        def _replace(match):
            img_id = match.group(2)
            if img_path := image_map.get(img_id):
                if EpubBuilder._add_image_to_epub(img_path, img_id, book, quality):
                    processed.add(img_id)
                    return (
                        '<div class="illust-container">'
                        f'<img src="images/{img_id}.jpg" alt="Image {img_id}"'
                        ' class="illust" />'
                        '</div>'
                    )
            logger.warning(
                f"EPUB 图片缺失: [{match.group(1)}:{img_id}] 未下载成功"
                " → 渲染为缺图占位框"
            )
            return EpubBuilder._missing_image_note(img_id)

        return _HAS_IMAGE_PATTERN.sub(_replace, content)

    @staticmethod
    def _missing_image_note(img_id: str) -> str:
        """Visible placeholder for an image that could not be embedded."""
        return (
            '<div class="illust-container">'
            f'<div class="illust-missing">（图片缺失 {html.escape(img_id)}）</div>'
            '</div>'
        )

    @staticmethod
    def _build_image_map(
        parent_dir: Path, novel_id: str, novel: NovelDraft
    ) -> dict[str, Path]:
        image_map: dict[str, Path] = {}
        known: list[tuple[str, str]] = []

        if isinstance(novel.images, dict):
            known.extend((k, "u") for k in novel.images.keys())
        if isinstance(novel.illusts, dict):
            known.extend((k, "p") for k in novel.illusts.keys())

        for img_id, img_type in known:
            for ext in (".jpg", ".png", ".jpeg", ".gif"):
                img_path = parent_dir / f"{novel_id}_{img_type}_{img_id}{ext}"
                if img_path.exists():
                    image_map[img_id] = img_path
                    break

        # Fallback: directory scan
        if not image_map:
            for f in parent_dir.iterdir():
                if not f.name.startswith(f"{novel_id}_"):
                    continue
                if f.suffix.lower() not in (".jpg", ".png", ".jpeg", ".gif"):
                    continue
                parts = f.stem.split("_")
                if len(parts) >= 3 and parts[1] in ("u", "p"):
                    image_map[parts[2]] = f

        return image_map

    # ------------------------------------------------------------------
    # Cover & CSS
    # ------------------------------------------------------------------

    @staticmethod
    def _find_cover(parent_dir: Path, novel_id: str) -> Path | None:
        for ext in (".jpg", ".png", ".jpeg"):
            candidate = parent_dir / f"{novel_id}_c_cover{ext}"
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _set_cover(book: epub.EpubBook, cover_path: Path | None) -> None:
        if not cover_path or not cover_path.exists():
            return
        try:
            with open(cover_path, "rb") as f:
                book.set_cover("cover.jpg", f.read())
        except Exception:
            logger.exception(f"Failed to set cover: {cover_path}")

    @staticmethod
    def _build_cover_page() -> epub.EpubHtml:
        cover_page = epub.EpubHtml(
            title="封面", file_name="cover_page.xhtml", lang="zh"
        )
        cover_page.content = (
            '<html><head>'
            '<title>Cover</title>'
            '<link rel="stylesheet" type="text/css" href="style/nav.css" />'
            '</head><body>'
            '<div class="cover-container">'
            '<img src="cover.jpg" alt="Cover" class="cover-image" />'
            '</div>'
            '</body></html>'
        )
        return cover_page

    @staticmethod
    def _build_main_page(
        title: str, author_name: str, content: str
    ) -> epub.EpubHtml:
        html_body = content.replace("\n", "<br/>")
        html_page = epub.EpubHtml(
            title="正文", file_name="content.xhtml", lang="zh"
        )
        html_page.content = (
            f'<html><head>'
            f'<title>{html.escape(title)}</title>'
            f'<link rel="stylesheet" type="text/css" href="style/nav.css" />'
            f'</head><body>'
            f'<h1>{html.escape(title)}</h1>'
            f'<p class="author">Author: {html.escape(author_name)}</p>'
            f'<hr/>'
            f'{html_body}'
            f'</body></html>'
        )
        return html_page
