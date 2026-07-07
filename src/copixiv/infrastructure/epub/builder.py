"""EPUB builder — converts downloaded novel text + images into an EPUB file."""

import html
import io
import re
from pathlib import Path

from PIL import Image
from ebooklib import epub

from copixiv.domain.services.filename import safe_filename
from copixiv.domain.services.language import has_image_placeholders

from copixiv.app.logger import logger

# Pattern for embedded image placeholders — keep in sync with domain.services.language
_HAS_IMAGE_PATTERN = re.compile(
    r"\[(uploadedimage|pixivimage):([\d\-]+)\]"
)

CSS_STYLE = """
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 5%; text-align: justify; }
h1 { text-align: center; }
.author { text-align: center; font-style: italic; margin-bottom: 2em; }
.illust-container { text-align: center; margin: 1em 0; }
.illust { max-width: 100%; height: auto; }
.cover-container { text-align: center; height: 100%; display: flex; justify-content: center; align-items: center; }
.cover-image { max-width: 100%; max-height: 100%; object-fit: contain; }
"""


class EpubBuilder:
    """Creates EPUB files from downloaded novel text and images."""

    def create_epub(self, data: dict, compress_quality: int = 75) -> bool:
        """Build an EPUB from the novel *data* dict.

        Returns True if the EPUB was written successfully.
        """
        path_str = data.get("path")
        if not path_str:
            logger.error("No path provided in data for EPUB creation")
            return False

        novel_path = Path(path_str)
        if not novel_path.exists():
            logger.error(f"Source text file not found: {novel_path}")
            return False

        title = data.get("title", "Untitled")
        author_name = data.get("author_name") or str(
            data.get("author_id", "Unknown Author")
        )
        novel_id = str(data.get("id"))
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
        image_map = self._build_image_map(parent_dir, novel_id, data)
        processed_content = self._replace_image_placeholders(
            content, image_map, book, compress_quality
        )

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

        # Write
        safe_title = safe_filename(title)
        output_path = parent_dir / f"{safe_title}_{novel_id}.epub"
        try:
            epub.write_epub(output_path, book, {})
            logger.info(f"Made Epub: ({data['id']}){data['title']}")
            return True
        except Exception:
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
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
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
        processed: set[str] = set()

        def _replace(match):
            img_id = match.group(2)
            if img_path := image_map.get(img_id):
                if EpubBuilder._add_image_to_epub(img_path, img_id, book, quality):
                    processed.add(img_id)
                else:
                    return match.group(0)
                return (
                    '<div class="illust-container">'
                    f'<img src="images/{img_id}.jpg" alt="Image {img_id}" class="illust" />'
                    '</div>'
                )
            return match.group(0)

        return _HAS_IMAGE_PATTERN.sub(_replace, content)

    @staticmethod
    def _build_image_map(
        parent_dir: Path, novel_id: str, data: dict
    ) -> dict[str, Path]:
        image_map: dict[str, Path] = {}
        known: list[tuple[str, str]] = []

        if isinstance(data.get("images"), dict):
            known.extend((k, "u") for k in data["images"].keys())
        if isinstance(data.get("illusts"), dict):
            known.extend((k, "p") for k in data["illusts"].keys())

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
