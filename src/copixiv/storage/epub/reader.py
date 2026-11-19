"""EPUB 读取 —— 用标准库 ``zipfile`` + ``xml.etree.ElementTree`` 解析正文与元数据。"""

from __future__ import annotations

import codecs
import html
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, unquote

from copixiv.core import naming as formats
from copixiv.log import logger
from copixiv.storage.encoding import decode_text_bytes

EPUB_INVALID_ERROR = "不是有效的 EPUB 文件"
EPUB_EMPTY_ERROR = "EPUB 中没有可提取的正文"
EPUB_TOO_LARGE_ERROR = "EPUB 文件过大"

MAX_METADATA_CHARS = 200
MAX_CONTAINER_BYTES = 128 * 1024 * 1024
MAX_BODY_CHARS = 20_000_000

_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_MIMETYPE_BYTES = 64
_MAX_CONTAINER_XML_BYTES = 1 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024

_DECLARED_ENCODING_RE = re.compile(
    rb"""(?:encoding|charset)\s*=\s*["']?\s*([A-Za-z0-9._-]+)""",
    re.IGNORECASE,
)
_XML_DECL_ENCODING_RE = re.compile(
    r'^(<\?xml\b[^>]*?)\s+encoding\s*=\s*(?:"[^"]*"|\'[^\']*\')([^>]*\?>)',
    re.IGNORECASE,
)

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_SKIP_TAGS = frozenset({"head", "script", "style", "title"})

_BODY_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_XHTML_SUFFIXES = frozenset({".xhtml", ".html", ".htm"})

_WS_RE = re.compile(r"[ \t\f\v]+")

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"


class EpubReadError(ValueError):
    """EPUB 读不出来 / 没有正文（调用方按单条 ``failed`` 处理，不写任何库表）。"""


@dataclass(frozen=True)
class EpubContent:
    """一次成功读取的 EPUB：正文 + 内嵌元数据（缺失为 ``None``）。"""

    body: str
    title: str | None = None
    author_name: str | None = None


def _parse_xml(raw: bytes) -> ET.Element:
    """Parse XML bytes via stdlib ElementTree, decoding multi-byte declarations."""
    try:
        return ET.fromstring(raw)
    except ValueError:
        # Expat rejects non-UTF8 multi-byte encodings (e.g. ``encoding="gbk"``)
        # in the XML declaration; decode to str and strip the encoding attr.
        text = decode_text_bytes(
            raw,
            declared_encoding=_declared_encoding(raw),
            strict=False,
        )
        text = _XML_DECL_ENCODING_RE.sub(r"\1\2", text.lstrip("\ufeff"), count=1)
        return ET.fromstring(text)


def _zip_mimetype_is_epub(data: bytes) -> bool:
    """该字节流是不是一个 zip，且 ``mimetype`` 条目写着 ``application/epub+zip``。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            with archive.open("mimetype") as handle:
                raw = handle.read(_MAX_MIMETYPE_BYTES + 1)
            return len(raw) <= _MAX_MIMETYPE_BYTES and raw.strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def looks_like_epub(name: str, data: bytes) -> bool:
    """是否按 EPUB 处理（``ingest`` 的分发判据）。"""
    if name.lower().endswith(formats.suffix_for(formats.EPUB)):
        return True
    return data[:4] == b"PK\x03\x04" and _zip_mimetype_is_epub(data)


def read_epub(data: bytes) -> EpubContent:
    """读一份 EPUB，返回正文 + 元数据；失败抛 :class:`EpubReadError`."""
    if not data:
        raise EpubReadError(EPUB_INVALID_ERROR)
    if len(data) > MAX_CONTAINER_BYTES:
        raise EpubReadError(EPUB_TOO_LARGE_ERROR)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            package = _read_package(archive)
            body = _extract_body(archive, package)
    except EpubReadError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, OSError) as exc:
        logger.warning("EPUB 读取失败：{}: {}", type(exc).__name__, exc)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc
    except ET.ParseError as exc:
        logger.warning("EPUB 的 XML 解析失败：{}", exc)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc
    except Exception as exc:  # noqa: BLE001 —— 不可信输入，仍按单条失败
        logger.exception("读取 EPUB 时发生预期外异常：{}", type(exc).__name__)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc

    if len(body) > MAX_BODY_CHARS:
        raise EpubReadError(EPUB_TOO_LARGE_ERROR)
    if not body:
        raise EpubReadError(EPUB_EMPTY_ERROR)

    return EpubContent(
        body=body,
        title=package.title,
        author_name=package.author_name,
    )


@dataclass(frozen=True)
class _Package:
    spine: tuple[str, ...]
    title: str | None
    author_name: str | None


def _opf_path(container_xml: bytes) -> str:
    """``META-INF/container.xml`` → OPF 在 zip 里的路径（取不到返回空串）。"""
    root = _parse_xml(container_xml)
    fallback = ""
    for rootfile in root.iter(f"{{{_CONTAINER_NS}}}rootfile"):
        full_path = (rootfile.get("full-path") or "").strip()
        if not full_path:
            continue
        if rootfile.get("media-type") == "application/oebps-package+xml":
            return posixpath.normpath(full_path.lstrip("/"))
        fallback = fallback or full_path
    return posixpath.normpath(fallback.lstrip("/")) if fallback else ""


def _resolve_opf_href(opf_dir: str, href: str) -> str:
    """OPF 的相对 ``href`` → zip 成员名。"""
    target = (href or "").split("#", 1)[0].strip()
    if not target or "://" in target:
        return ""
    return posixpath.normpath(posixpath.join(opf_dir, target))


def _manifest(
    opf_root: ET.Element,
    opf_dir: str,
) -> dict[str, tuple[str, str, frozenset[str]]]:
    """``id → (成员路径, media-type, properties)``."""
    entries: dict[str, tuple[str, str, frozenset[str]]] = {}
    for item in opf_root.iter(f"{{{_OPF_NS}}}item"):
        item_id = item.get("id")
        if not item_id:
            continue
        media_type = (item.get("media-type") or "").lower()
        props = frozenset((item.get("properties") or "").split())
        path = _resolve_opf_href(opf_dir, item.get("href") or "")
        if path:
            entries[item_id] = (path, media_type, props)
    return entries


def _spine_paths(
    opf_root: ET.Element,
    manifest: dict[str, tuple[str, str, frozenset[str]]],
) -> tuple[str, ...]:
    """spine 顺序里的正文文档路径（跳过 nav / cover / 非正文）。"""
    paths: list[str] = []
    seen: set[str] = set()
    for itemref in opf_root.iter(f"{{{_OPF_NS}}}itemref"):
        entry = manifest.get(itemref.get("idref") or "")
        if entry is None:
            continue
        path, media_type, props = entry
        if path in seen or not _is_body_document(path, media_type, props):
            continue
        seen.add(path)
        paths.append(path)

    if paths:
        return tuple(paths)
    for path, media_type, props in manifest.values():
        if path not in seen and _is_body_document(path, media_type, props):
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def _is_body_document(path: str, media_type: str, props: frozenset[str]) -> bool:
    if "nav" in props or "cover" in props:
        return False
    return posixpath.splitext(path)[1].lower() in _XHTML_SUFFIXES or media_type in _BODY_MEDIA_TYPES


def _first_dc(opf_root: ET.Element, name: str, *, prefer_role: str | None = None) -> str | None:
    """``dc:<name>`` 的第一个可用值；``prefer_role`` 命中时优先它。"""
    candidates: list[tuple[str, str]] = []
    for element in opf_root.iter(f"{{{_DC_NS}}}{name}"):
        text = (element.text or "").strip()
        if not text:
            continue
        cleaned = _clean_metadata(text)
        if cleaned:
            candidates.append((cleaned, _role_of(element)))
    if not candidates:
        return None
    if prefer_role:
        for cleaned, role in candidates:
            if role == prefer_role:
                return cleaned
    return candidates[0][0]


def _role_of(element: ET.Element) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == "role":
            return str(value).strip().lower()
    return ""


def _clean_metadata(value: str) -> str:
    text = re.sub(r"<[^>]*>", " ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()
    return text[:MAX_METADATA_CHARS]


def _read_package(archive: zipfile.ZipFile) -> _Package:
    container_xml = _read_member(
        archive,
        "META-INF/container.xml",
        max_bytes=_MAX_CONTAINER_XML_BYTES,
    )
    opf_path = _opf_path(container_xml)
    if not opf_path:
        raise EpubReadError(EPUB_INVALID_ERROR)
    opf_dir = posixpath.dirname(opf_path)
    opf_xml = _read_member(archive, opf_path, max_bytes=_MAX_MEMBER_BYTES)
    opf_root = _parse_xml(opf_xml)
    manifest = _manifest(opf_root, opf_dir)
    return _Package(
        spine=_spine_paths(opf_root, manifest),
        title=_first_dc(opf_root, "title"),
        author_name=_first_dc(opf_root, "creator", prefer_role="aut"),
    )


def _extract_body(archive: zipfile.ZipFile, package: _Package) -> str:
    chunks: list[str] = []
    total_bytes = 0
    total_chars = 0
    for path in package.spine:
        remaining = _MAX_TOTAL_UNCOMPRESSED_BYTES - total_bytes
        if remaining <= 0:
            raise EpubReadError(EPUB_TOO_LARGE_ERROR)
        markup = _read_member(archive, path, max_bytes=remaining)
        total_bytes += len(markup)
        text = html_to_text(markup)
        if text:
            total_chars += len(text) + (1 if chunks else 0)
            if total_chars > MAX_BODY_CHARS:
                raise EpubReadError(EPUB_TOO_LARGE_ERROR)
            chunks.append(text)
    return "\n".join(chunks).strip()


def _read_member(
    archive: zipfile.ZipFile,
    path: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    limit = _MAX_MEMBER_BYTES if max_bytes is None else min(_MAX_MEMBER_BYTES, max_bytes)
    if limit <= 0:
        raise EpubReadError(EPUB_TOO_LARGE_ERROR)
    candidates = (path, unquote(path), quote(path, safe="/"))
    for candidate in dict.fromkeys(candidates):
        try:
            with archive.open(candidate) as handle:
                chunk = handle.read(limit + 1)
                if len(chunk) > limit:
                    raise EpubReadError(EPUB_TOO_LARGE_ERROR)
                return chunk
        except KeyError:
            continue
    raise KeyError(path)


class _TextExtractor(HTMLParser):
    """HTML → 纯文本：块级标签补换行，跳过 script / style / head。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag not in _SKIP_TAGS and tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _decode_markup(data: bytes) -> str:
    """XHTML 字节 → 字符串：优先声明编码，再走共享探测解码。"""
    return decode_text_bytes(
        data,
        declared_encoding=_declared_encoding(data),
        strict=False,
    )


def _declared_encoding(data: bytes) -> str | None:
    match = _DECLARED_ENCODING_RE.search(data[:1024])
    if not match:
        return None
    name = match.group(1).decode("ascii", "ignore")
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def html_to_text(markup: bytes) -> str:
    """一段 XHTML → 纯文本（一行一段，行内空白折叠）。"""
    parser = _TextExtractor()
    parser.feed(_decode_markup(markup))
    parser.close()

    lines = (_WS_RE.sub(" ", raw.replace("\xa0", " ")).strip() for raw in parser.text().split("\n"))
    return "\n".join(line for line in lines if line)


__all__ = [
    "EPUB_EMPTY_ERROR",
    "EPUB_INVALID_ERROR",
    "EPUB_TOO_LARGE_ERROR",
    "MAX_BODY_CHARS",
    "MAX_CONTAINER_BYTES",
    "MAX_METADATA_CHARS",
    "EpubContent",
    "EpubReadError",
    "html_to_text",
    "looks_like_epub",
    "read_epub",
]
