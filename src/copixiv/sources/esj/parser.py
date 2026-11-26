"""Pure stdlib HTML and reference parsers for ESJZone."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from copixiv.core.models import SOURCE_ESJ
from copixiv.core.sources import (
    ExternalAuthor,
    ExternalBookOutline,
    ExternalChapterEntry,
    ExternalSeries,
    ParsedSourceRef,
)

DEFAULT_ESJ_BASE_URL = "https://www.esjzone.cc"
_ESJ_BASE_DOMAINS = ("esjzone.cc", "esjzone.one", "esjzone.me")
_UTC_PLUS_8 = timezone(timedelta(hours=8))

_ESJ_PREFIX_BOOK_RE = re.compile(r"^esj:(\d+)$", re.IGNORECASE)
_ESJ_PREFIX_CHAPTER_RE = re.compile(r"^esj:(\d+)/(\d+)$", re.IGNORECASE)
_BARE_BOOK_RE = re.compile(r"^(\d+)$")
_BARE_CHAPTER_RE = re.compile(r"^(\d+)/(\d+)$")
_DETAIL_PATH_RE = re.compile(r"^/detail/(\d+)\.html$")
_CHAPTER_PATH_RE = re.compile(r"^/forum/(\d+)/(\d+)\.html$")
_PROFILE_UID_RE = re.compile(r"/my/profile\?(?:[^#]*&)?uid=(\d+)")
_JINJING_RE = re.compile(r"<JinJing>(.*?)</JinJing>", re.DOTALL | re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKwW萬万])?")
_DATETIME_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?")
_TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*ESJ\s*Zone\s*$", re.IGNORECASE)
_ASCII_WS_RE = re.compile(r"[ \t\r\n\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")

_TRANSLATION_GENRE_MARKERS = ("譯", "译", "日", "韓", "韩")
_ORIGINAL_GENRE_MARKERS = ("原創", "原创")
_CHALLENGE_STRONG_MARKERS = (
    "cf-turnstile",
    "challenge-platform",
    "__cf_chl_",
    "cf_chl_opt",
    "cf-challenge-running",
    "attention required! | cloudflare",
)
_ESJ_PAGE_MARKERS = ("book-detail", "forum-content", "chapterlist")
_PASSWORD_MARKERS = ("btn-send-pw", "內文目前施工中", "内文目前施工中")

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_PARA_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr"})
_LINE_BLOCK_TAGS = frozenset({"div", "li", "tr", "section", "article", "header", "footer"})
_CHROME_TAGS = frozenset({"nav", "header", "footer"})
_IGNORED_CONTENT_TAGS = frozenset({"script", "style"})


@dataclass(frozen=True)
class EsjParsedChapter:
    """Structured payload parsed from an ESJZone chapter page."""

    book_id: str
    chapter_id: str
    chapter_url: str
    title: str
    content: str
    images: dict[str, str]
    poster_uid: str | None = None
    poster_name: str | None = None
    create_time: datetime | None = None
    requires_password: bool = False


@dataclass(frozen=True)
class _TagFrame:
    tag: str
    classes: frozenset[str]
    id_: str


def is_esj_host(hostname: str | None) -> bool:
    """Return ``True`` when *hostname* belongs to an official ESJZone domain."""
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in _ESJ_BASE_DOMAINS)


def _book_ref(book_id: str) -> ParsedSourceRef | None:
    if int(book_id) <= 0:
        return None
    return ParsedSourceRef(
        source=SOURCE_ESJ,
        kind="book",
        series_key=book_id,
        chapter_key=None,
        normalized_ref=f"{DEFAULT_ESJ_BASE_URL}/detail/{book_id}.html",
    )


def _chapter_ref(book_id: str, chapter_id: str) -> ParsedSourceRef | None:
    if int(book_id) <= 0 or int(chapter_id) <= 0:
        return None
    return ParsedSourceRef(
        source=SOURCE_ESJ,
        kind="chapter",
        series_key=book_id,
        chapter_key=chapter_id,
        normalized_ref=f"{DEFAULT_ESJ_BASE_URL}/forum/{book_id}/{chapter_id}.html",
    )


def parse_esj_ref(raw: str, *, allow_bare_id: bool = False) -> ParsedSourceRef | None:
    """Parse an ESJZone book/chapter URL, ``esj:`` ref, or bare ID when allowed."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None

    if match := _ESJ_PREFIX_BOOK_RE.match(cleaned):
        return _book_ref(match.group(1))
    if match := _ESJ_PREFIX_CHAPTER_RE.match(cleaned):
        return _chapter_ref(match.group(1), match.group(2))

    if allow_bare_id:
        if match := _BARE_BOOK_RE.match(cleaned):
            return _book_ref(match.group(1))
        if match := _BARE_CHAPTER_RE.match(cleaned):
            return _chapter_ref(match.group(1), match.group(2))

    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not is_esj_host(parsed.hostname):
        return None

    path = parsed.path.rstrip("/")
    if match := _DETAIL_PATH_RE.match(path):
        return _book_ref(match.group(1))
    if match := _CHAPTER_PATH_RE.match(path):
        return _chapter_ref(match.group(1), match.group(2))

    return None


def is_original_genre(genre: str | None) -> bool:
    """Return ``True`` when an ESJ ``類型`` field marks an original work."""
    if not genre:
        return False
    text = genre.strip()
    if not text:
        return False
    if any(marker in text for marker in _TRANSLATION_GENRE_MARKERS):
        return False
    return any(marker in text for marker in _ORIGINAL_GENRE_MARKERS)


def build_esj_author_key(
    author_name: str | None,
    *,
    uid: str | None = None,
    is_original: bool = False,
    genre: str | None = None,
) -> str:
    """Build ``author.source_key``: ``uid:{uid}`` for originals with uid, else ``name:{name}``."""
    effective_original = is_original if genre is None else is_original_genre(genre)
    clean_uid = (uid or "").strip()
    if effective_original and clean_uid:
        return f"uid:{clean_uid}"
    clean_name = (author_name or "").strip() or "佚名"
    return f"name:{clean_name}"


def is_challenge_html(html: str) -> bool:
    """Detect Cloudflare Turnstile / JS challenge / WAF interstitial pages."""
    if not html:
        return False
    lowered = html.lower()
    if any(marker in lowered for marker in _CHALLENGE_STRONG_MARKERS):
        return True
    if "just a moment" in lowered:
        if "<title>just a moment" in lowered:
            return True
        if not any(page_marker in lowered for page_marker in _ESJ_PAGE_MARKERS):
            return True
    return False


def is_password_challenge_html(html: str) -> bool:
    """Detect ESJZone chapter password lock markers."""
    if not html:
        return False
    return any(marker in html for marker in _PASSWORD_MARKERS)


def extract_jinjing_token(html: str) -> str | None:
    """Extract the ``<JinJing>...</JinJing>`` authorization token from unlock responses."""
    if not html:
        return None
    match = _JINJING_RE.search(html)
    if not match:
        return None
    token = match.group(1).strip()
    return token or None


def parse_esj_count(raw: str) -> int:
    """Parse ESJ integer/comma/K/W counters into a non-negative integer."""
    if not raw:
        return 0
    cleaned = raw.replace(",", "").strip()
    match = _COUNT_RE.search(cleaned)
    if not match:
        return 0
    value = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    multiplier = 1
    if suffix == "k":
        multiplier = 1_000
    elif suffix in ("w", "萬", "万"):
        multiplier = 10_000
    return max(0, round(value * multiplier))


def parse_esj_datetime(text: str) -> datetime | None:
    """Parse ``YYYY-MM-DD HH:MM:SS`` or ``YYYY/MM/DD`` into a UTC+8 aware ``datetime``."""
    if not text:
        return None
    match = _DATETIME_RE.search(text)
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour = int(match.group(4)) if match.group(4) is not None else 0
    minute = int(match.group(5)) if match.group(5) is not None else 0
    second = int(match.group(6)) if match.group(6) is not None else 0
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=_UTC_PLUS_8)
    except ValueError:
        return None


def _attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {k.lower(): (v if v is not None else "") for k, v in attrs}


def _pick_image_url(attrs: dict[str, str]) -> str | None:
    for key in ("data-src", "data-original", "src"):
        candidate = attrs.get(key, "").strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered.startswith(("data:", "javascript:")):
            continue
        return candidate
    return None


def _normalize_inline_ws(text: str) -> str:
    return _ASCII_WS_RE.sub(" ", text).strip()


def _extract_uid_from_href(href: str) -> str | None:
    match = _PROFILE_UID_RE.search(href)
    return match.group(1) if match else None


def _is_internal_chapter_url(abs_url: str, book_id: str, base_host: str | None) -> str | None:
    parsed = urlparse(abs_url)
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    hostname = parsed.hostname.lower() if parsed.hostname else None
    if not is_esj_host(hostname) and (base_host is None or hostname != base_host):
        return None
    match = _CHAPTER_PATH_RE.match(parsed.path.rstrip("/"))
    if not match or match.group(1) != book_id:
        return None
    chapter_id = match.group(2)
    return chapter_id if int(chapter_id) > 0 else None


class _BookOutlineHTMLParser(HTMLParser):
    """Stdlib HTMLParser extracting ESJZone book metadata, tags, cover, and TOC."""

    def __init__(self, book_id: str, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._book_id = book_id
        self._base_url = base_url
        parsed_base = urlparse(base_url)
        self._base_host = parsed_base.hostname.lower() if parsed_base.hostname else None

        self._stack: list[_TagFrame] = []
        self._book_detail_depth = 0
        self._gallery_depth = 0
        self._tag_section_depth = 0
        self._chapter_list_depth = 0

        self._in_title_h2 = False
        self._title_parts: list[str] = []
        self.title: str = ""

        self._in_meta_li = False
        self._li_parts: list[str] = []
        self._li_uid: str | None = None
        self.genre: str = ""
        self.author_name: str = ""
        self.detail_uid: str | None = None

        self._vtimes_depth = 0
        self._vtimes_parts: list[str] = []
        self.view: int | None = None

        self._favorite_depth = 0
        self._favorite_parts: list[str] = []
        self.like: int | None = None

        self.cover_url: str | None = None

        self._in_tag_link = False
        self._tag_parts: list[str] = []
        self._seen_tags: set[str] = set()
        self.tags: list[str] = []

        self._in_chapter_link = False
        self._chapter_href: str = ""
        self._chapter_title_attr: str = ""
        self._chapter_parts: list[str] = []
        self.chapter_entries: list[ExternalChapterEntry] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = _attrs_dict(attrs)
        classes = frozenset(attr_map.get("class", "").split())
        id_ = attr_map.get("id", "").strip()

        if tag == "img":
            if self._gallery_depth > 0 and self.cover_url is None:
                raw_img = _pick_image_url(attr_map)
                if raw_img is not None:
                    self.cover_url = urljoin(self._base_url, raw_img)
            return

        if tag in _VOID_TAGS:
            return

        self._stack.append(_TagFrame(tag=tag, classes=classes, id_=id_))

        if "book-detail" in classes:
            self._book_detail_depth += 1
        if "product-gallery" in classes:
            self._gallery_depth += 1
        if tag == "section" and "widget-tags" in classes:
            self._tag_section_depth += 1
        if id_ == "chapterList":
            self._chapter_list_depth += 1
        if (id_ == "vtimes" and self.view is None) or self._vtimes_depth > 0:
            self._vtimes_depth += 1
        if (id_ == "favorite" and self.like is None) or self._favorite_depth > 0:
            self._favorite_depth += 1

        if self._book_detail_depth > 0:
            if tag == "h2" and not self.title and not self._in_title_h2:
                self._in_title_h2 = True
                self._title_parts.clear()
            elif tag == "li" and not self._in_meta_li:
                self._in_meta_li = True
                self._li_parts.clear()
                self._li_uid = None
            elif self._in_meta_li and tag == "a" and self._li_uid is None:
                uid = _extract_uid_from_href(attr_map.get("href", ""))
                if uid:
                    self._li_uid = uid

        if self._tag_section_depth > 0 and tag == "a" and not self._in_tag_link:
            self._in_tag_link = True
            self._tag_parts.clear()

        if self._chapter_list_depth > 0 and tag == "a" and not self._in_chapter_link:
            self._in_chapter_link = True
            self._chapter_href = attr_map.get("href", "").strip()
            self._chapter_title_attr = (attr_map.get("title") or attr_map.get("data-title") or "").strip()
            self._chapter_parts.clear()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title_h2:
            self._title_parts.append(data)
        if self._in_meta_li:
            self._li_parts.append(data)
        if self._vtimes_depth > 0:
            self._vtimes_parts.append(data)
        if self._favorite_depth > 0:
            self._favorite_parts.append(data)
        if self._in_tag_link:
            self._tag_parts.append(data)
        if self._in_chapter_link:
            self._chapter_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx].tag == tag:
                while len(self._stack) > idx:
                    frame = self._stack.pop()
                    self._exit_frame(frame)
                break

    def _exit_frame(self, frame: _TagFrame) -> None:
        if self._in_title_h2 and frame.tag == "h2":
            self._in_title_h2 = False
            candidate = _normalize_inline_ws("".join(self._title_parts))
            if candidate and not self.title:
                self.title = candidate
            self._title_parts.clear()

        if self._in_meta_li and frame.tag == "li":
            self._in_meta_li = False
            self._commit_meta_li()

        if self._vtimes_depth > 0:
            self._vtimes_depth -= 1
            if self._vtimes_depth == 0 and self.view is None:
                self.view = parse_esj_count("".join(self._vtimes_parts))
                self._vtimes_parts.clear()

        if self._favorite_depth > 0:
            self._favorite_depth -= 1
            if self._favorite_depth == 0 and self.like is None:
                self.like = parse_esj_count("".join(self._favorite_parts))
                self._favorite_parts.clear()

        if self._in_tag_link and frame.tag == "a":
            self._in_tag_link = False
            tag_text = _normalize_inline_ws("".join(self._tag_parts))
            self._tag_parts.clear()
            if tag_text and tag_text not in self._seen_tags:
                self._seen_tags.add(tag_text)
                self.tags.append(tag_text)

        if self._in_chapter_link and frame.tag == "a":
            self._in_chapter_link = False
            self._commit_chapter_link()

        if "book-detail" in frame.classes:
            self._book_detail_depth = max(0, self._book_detail_depth - 1)
        if "product-gallery" in frame.classes:
            self._gallery_depth = max(0, self._gallery_depth - 1)
        if frame.tag == "section" and "widget-tags" in frame.classes:
            self._tag_section_depth = max(0, self._tag_section_depth - 1)
        if frame.id_ == "chapterList":
            self._chapter_list_depth = max(0, self._chapter_list_depth - 1)

    def _commit_meta_li(self) -> None:
        text = _normalize_inline_ws("".join(self._li_parts))
        self._li_parts.clear()
        uid = self._li_uid
        self._li_uid = None
        if not text:
            return
        sep_idx = -1
        for sep in ("：", ":"):
            pos = text.find(sep)
            if pos > 0 and (sep_idx == -1 or pos < sep_idx):
                sep_idx = pos
        if sep_idx <= 0:
            return
        label = text[:sep_idx].strip()
        value = text[sep_idx + 1 :].strip()
        if label in ("類型", "类型") and not self.genre:
            self.genre = value
        elif label == "作者" and not self.author_name:
            self.author_name = value
            if uid and self.detail_uid is None:
                self.detail_uid = uid

    def _commit_chapter_link(self) -> None:
        href = self._chapter_href
        title_attr = self._chapter_title_attr
        text = _normalize_inline_ws("".join(self._chapter_parts))
        self._chapter_href = ""
        self._chapter_title_attr = ""
        self._chapter_parts.clear()

        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            return

        series_index = len(self.chapter_entries) + 1
        title = text or title_attr or f"第{series_index}章"
        abs_url = urljoin(self._base_url, href)
        chapter_id = _is_internal_chapter_url(abs_url, self._book_id, self._base_host)
        if chapter_id is not None:
            self.chapter_entries.append(
                ExternalChapterEntry(
                    source_key=f"{self._book_id}/{chapter_id}",
                    title=title,
                    series_index=series_index,
                    is_external_link=False,
                    external_url=None,
                )
            )
            return

        self.chapter_entries.append(
            ExternalChapterEntry(
                source_key=abs_url,
                title=title,
                series_index=series_index,
                is_external_link=True,
                external_url=abs_url,
            )
        )


def parse_book_outline(html: str, book_id: str, base_url: str) -> ExternalBookOutline:
    """Parse an ESJZone book detail page into an :class:`ExternalBookOutline`."""
    parser = _BookOutlineHTMLParser(book_id=book_id, base_url=base_url)
    parser.feed(html)
    parser.close()

    genre = parser.genre
    is_original = is_original_genre(genre)
    clean_author = parser.author_name.strip() or "佚名"
    author_key = build_esj_author_key(
        clean_author,
        uid=parser.detail_uid,
        is_original=is_original,
    )
    author = ExternalAuthor(source_key=author_key, name=clean_author)

    series = ExternalSeries(
        source_key=book_id,
        title=parser.title or None,
        cover_url=parser.cover_url,
        author=author,
    )
    return ExternalBookOutline(
        series=series,
        chapter_entries=tuple(parser.chapter_entries),
        author=author,
        tags=tuple(parser.tags),
        like=parser.like or 0,
        view=parser.view or 0,
        extra={
            "genre": genre,
            "author_name": clean_author,
            "is_original": is_original,
            "detail_uid": parser.detail_uid,
        },
    )


class _ChapterHTMLParser(HTMLParser):
    """Stdlib HTMLParser extracting ESJZone chapter metadata, text, and images."""

    def __init__(self, image_base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._image_base_url = image_base_url
        self._stack: list[_TagFrame] = []

        self._chrome_depth = 0
        self._meta_depth = 0
        self._content_depth = 0
        self._content_captured = False
        self._ignored_depth = 0
        self._pre_depth = 0

        self._in_title_tag = False
        self._head_title_parts: list[str] = []
        self.head_title: str = ""

        self._in_heading: str | None = None
        self._heading_parts: list[str] = []
        self.h2_title: str = ""
        self.h1_title: str = ""

        self._in_meta_link = False
        self._meta_link_uid: str | None = None
        self._meta_link_parts: list[str] = []
        self._meta_text_parts: list[str] = []
        self.poster_uid: str | None = None
        self.poster_name: str | None = None
        self.create_time: datetime | None = None

        self._body_parts: list[str] = []
        self.images: dict[str, str] = {}
        self._image_counter = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = _attrs_dict(attrs)
        classes = frozenset(attr_map.get("class", "").split())
        id_ = attr_map.get("id", "").strip()

        if tag in _VOID_TAGS:
            if self._content_depth > 0 and self._ignored_depth == 0:
                if tag == "br":
                    self._body_parts.append("\n")
                elif tag == "hr":
                    self._body_parts.append("\n\n")
                elif tag == "img":
                    raw_src = _pick_image_url(attr_map)
                    if raw_src is not None:
                        self._image_counter += 1
                        key = str(self._image_counter)
                        self.images[key] = urljoin(self._image_base_url, raw_src)
                        self._body_parts.append(f"[uploadedimage:{key}]")
            return

        self._stack.append(_TagFrame(tag=tag, classes=classes, id_=id_))

        if tag in _CHROME_TAGS:
            self._chrome_depth += 1

        if tag == "title" and not self.head_title:
            self._in_title_tag = True
            self._head_title_parts.clear()

        if "single-post-meta" in classes and self._meta_depth == 0:
            self._meta_depth = 1
        elif self._meta_depth > 0:
            self._meta_depth += 1

        if self._meta_depth > 0 and tag == "a" and self.poster_uid is None:
            uid = _extract_uid_from_href(attr_map.get("href", ""))
            if uid:
                self._in_meta_link = True
                self._meta_link_uid = uid
                self._meta_link_parts.clear()

        if self._content_depth == 0 and not self._content_captured and "forum-content" in classes:
            self._content_depth = 1
        elif self._content_depth > 0:
            self._content_depth += 1
            if tag in _IGNORED_CONTENT_TAGS:
                self._ignored_depth += 1
            elif self._ignored_depth == 0:
                if tag == "pre":
                    self._pre_depth += 1
                if tag in _PARA_TAGS:
                    self._body_parts.append("\n\n")
                elif tag in _LINE_BLOCK_TAGS:
                    self._body_parts.append("\n")

        if (
            self._content_depth == 0
            and self._chrome_depth == 0
            and tag in ("h1", "h2")
            and self._in_heading is None
        ):
            self._in_heading = tag
            self._heading_parts.clear()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title_tag:
            self._head_title_parts.append(data)
        if self._in_heading is not None:
            self._heading_parts.append(data)
        if self._meta_depth > 0:
            self._meta_text_parts.append(data)
            if self._in_meta_link:
                self._meta_link_parts.append(data)
        if self._content_depth > 0 and self._ignored_depth == 0:
            text = data.replace("\xa0", " ")
            if self._pre_depth == 0:
                text = _ASCII_WS_RE.sub(" ", text)
            self._body_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx].tag == tag:
                while len(self._stack) > idx:
                    frame = self._stack.pop()
                    self._exit_frame(frame)
                break

    def _exit_frame(self, frame: _TagFrame) -> None:
        if self._in_title_tag and frame.tag == "title":
            self._in_title_tag = False
            self.head_title = _normalize_inline_ws("".join(self._head_title_parts))
            self._head_title_parts.clear()

        if self._in_heading is not None and frame.tag == self._in_heading:
            text = _normalize_inline_ws("".join(self._heading_parts))
            self._heading_parts.clear()
            if text:
                if self._in_heading == "h2" and not self._content_captured:
                    self.h2_title = text
                elif self._in_heading == "h1" and not self._content_captured and not self.h1_title:
                    self.h1_title = text
            self._in_heading = None

        if self._in_meta_link and frame.tag == "a":
            self._in_meta_link = False
            self.poster_uid = self._meta_link_uid
            self.poster_name = _normalize_inline_ws("".join(self._meta_link_parts)) or None
            self._meta_link_uid = None
            self._meta_link_parts.clear()

        if self._meta_depth > 0:
            self._meta_depth -= 1
            if self._meta_depth == 0 and self.create_time is None:
                meta_text = _normalize_inline_ws(" ".join(self._meta_text_parts))
                self.create_time = parse_esj_datetime(meta_text)
                self._meta_text_parts.clear()

        if self._content_depth > 0:
            is_outer = self._content_depth == 1
            if not is_outer:
                if frame.tag in _IGNORED_CONTENT_TAGS:
                    self._ignored_depth = max(0, self._ignored_depth - 1)
                elif self._ignored_depth == 0:
                    if frame.tag in _PARA_TAGS:
                        self._body_parts.append("\n\n")
                    elif frame.tag in _LINE_BLOCK_TAGS:
                        self._body_parts.append("\n")
                    if frame.tag == "pre":
                        self._pre_depth = max(0, self._pre_depth - 1)
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_captured = True

        if frame.tag in _CHROME_TAGS:
            self._chrome_depth = max(0, self._chrome_depth - 1)

    def render_content(self) -> str:
        """Assemble normalized plain text preserving paragraph structure."""
        raw = "".join(self._body_parts)
        lines = [line.strip(" \t") for line in raw.split("\n")]
        cleaned = "\n".join(lines)
        return _MULTI_NL_RE.sub("\n\n", cleaned).strip("\n")

    def resolve_title(self, chapter_id: str) -> str:
        """Resolve best chapter title from post heading, page title, or fallback."""
        if self.h2_title:
            return self.h2_title
        if self.h1_title:
            return self.h1_title
        if self.head_title:
            stripped = _TITLE_SUFFIX_RE.sub("", self.head_title).strip()
            if stripped:
                return stripped
        return f"第{chapter_id}章"


def parse_chapter(
    html: str,
    book_id: str,
    chapter_id: str,
    chapter_url: str,
    base_url: str,
) -> EsjParsedChapter:
    """Parse an ESJZone chapter HTML page into text, images, and poster metadata."""
    image_base = chapter_url or base_url
    parser = _ChapterHTMLParser(image_base_url=image_base)
    parser.feed(html)
    parser.close()

    return EsjParsedChapter(
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_url=chapter_url,
        title=parser.resolve_title(chapter_id),
        content=parser.render_content(),
        images=parser.images,
        poster_uid=parser.poster_uid,
        poster_name=parser.poster_name,
        create_time=parser.create_time,
        requires_password=is_password_challenge_html(html),
    )


__all__ = [
    "DEFAULT_ESJ_BASE_URL",
    "EsjParsedChapter",
    "build_esj_author_key",
    "extract_jinjing_token",
    "is_challenge_html",
    "is_esj_host",
    "is_original_genre",
    "is_password_challenge_html",
    "parse_book_outline",
    "parse_chapter",
    "parse_esj_count",
    "parse_esj_datetime",
    "parse_esj_ref",
]
