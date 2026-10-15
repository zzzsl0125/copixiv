"""EPUB 读取 —— 把 ``.epub`` 字节流读成入库管线要的正文 + 元数据。

职责只有一个：给定字节流，返回**正文**、EPUB 内嵌的**标题 / 作者**（可能
缺失）。不碰数据库、不碰磁盘、不做去重、不发号；那些都是
:mod:`copixiv.features.library.ingest` 的事。

放在 ``storage/epub/`` 与 :mod:`copixiv.storage.epub.builder`（**写**）对称：
格式机制归 storage，``features/library`` 只决定"这个上传该不该走这条读路径"。
将来阅读器 / 监控目录导入都能复用本模块，不必从上传切片里搬。

与 TXT 路径的关系（契约，别改）：本模块产出的 ``body`` 仍要经过
``ingest.normalize_text`` 与 ``body_fingerprint``，所以 EPUB 与 TXT 共用同一套
「规范化 → sha256 去重」语义；EPUB 只是多带了一个标题/作者来源和一份原件。
"""

from __future__ import annotations

import codecs
import html
import io
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser

from ebooklib import epub
from lxml import etree

from copixiv.core import formats
from copixiv.log import logger

EPUB_INVALID_ERROR = "不是有效的 EPUB 文件"
EPUB_EMPTY_ERROR = "EPUB 中没有可提取的正文"
EPUB_TOO_LARGE_ERROR = "EPUB 文件过大"

# 元数据（标题 / 作者）落库前的长度上限：``dc:title`` 里塞进整段简介的 EPUB
# 并不罕见，而 ``novel.title`` 会进文件名（``local_novel_path`` 已按 NAME_MAX
# 截断）与列表标题。截断比让一条脏元数据撑爆路径名好。
MAX_METADATA_CHARS = 200

# 输入字节上限（压缩包本身）。解析前就拦，否则一个几百 MB 的容器会在
# ``read_epub`` 里被整个解进内存 —— 上传是同步请求，不能让它拖垮写事务。
MAX_EPUB_BYTES = 50 * 1024 * 1024

# 解压后正文上限（字符）。它防的是"把一本书的正文撑爆 text 列 / FTS"，**不是**
# 内存保护（那由 MAX_EPUB_BYTES 负责）：这个检查发生在解析之后。
MAX_BODY_CHARS = 20_000_000

# ``read_epub`` 预期内的读取失败：畸形 / 损坏 / 结构不对的容器。之外的异常
# （例如 ebooklib 版本问题导致的 AttributeError）不该被伪装成"不是 EPUB"，
# 见 :func:`read_epub` 的第二个 except。
_EXPECTED_READ_ERRORS = (
    epub.EpubException,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    etree.XMLSyntaxError,
    KeyError,
    OSError,
)

# XHTML 里声明编码的两种写法（``<?xml encoding="gbk"?>`` / ``<meta charset=…>``）。
_DECLARED_ENCODING_RE = re.compile(
    rb"""(?:encoding|charset)\s*=\s*["']?\s*([A-Za-z0-9._-]+)""", re.IGNORECASE,
)

# 块级标签：进 / 出时各补一个换行，保住段落边界（否则整章会挤成一行）。
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "br", "caption",
    "dd", "div", "dl", "dt", "figcaption", "figure", "footer", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
})
# 这些标签里的文本不是正文（``title`` 会与元数据重复，``script``/``style``
# 是代码）。
_SKIP_TAGS = frozenset({"head", "script", "style", "title"})

# 正文文档的 media_type：规范值 + 现实里常见的错写（见 ``_is_body_document``）。
_BODY_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})

_WS_RE = re.compile(r"[ \t\f\v]+")


class EpubReadError(ValueError):
    """EPUB 读不出来 / 没有正文（调用方按单条 ``failed`` 处理，不写任何库表）。"""


@dataclass(frozen=True)
class EpubContent:
    """一次成功读取的 EPUB：正文 + 内嵌元数据（缺失为 ``None``）。

    ``body`` **未规范化**（换行 / 首尾空白原样）——调用方与 TXT 路径一样走
    ``ingest.normalize_text``，两条路共用同一套规范化，指纹才可比。
    """

    body: str
    title: str | None = None
    author_name: str | None = None


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _zip_mimetype_is_epub(data: bytes) -> bool:
    """该字节流是不是一个 zip，且 ``mimetype`` 条目写着 ``application/epub+zip``。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.read("mimetype").strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def looks_like_epub(name: str, data: bytes) -> bool:
    """是否按 EPUB 处理（``ingest`` 的分发判据）。

    后缀优先（用户在文件选择器里就是按后缀选的）；**另外**接受内容嗅探 ——
    有些下载站把 .epub 存成别的后缀、或用户手动改名，只要 ``mimetype`` 条目
    对得上就仍按 EPUB 读。``.txt`` 文件不可能同时是这种 zip，所以不误伤。
    """
    if name.lower().endswith(formats.suffix_for(formats.EPUB)):
        return True
    return data[:4] == b"PK\x03\x04" and _zip_mimetype_is_epub(data)


def read_epub(data: bytes) -> EpubContent:
    """读一份 EPUB，返回正文 + 元数据；失败抛 :class:`EpubReadError`。

    正文按 **spine 顺序**取所有 XHTML 文档（含 ``<p>`` 一类块级标签的段落
    边界），跳过 nav / ncx / 封面页。图片、样式、字体一律不读 —— 这本管理器
    只索引正文；原件本身照原样另存（``ingest`` 负责），排版不会丢。
    """
    if not data:
        raise EpubReadError(EPUB_INVALID_ERROR)
    if len(data) > MAX_EPUB_BYTES:
        raise EpubReadError(EPUB_TOO_LARGE_ERROR)

    try:
        book = epub.read_epub(io.BytesIO(data))
    except _EXPECTED_READ_ERRORS as exc:
        logger.warning("EPUB 读取失败：{}: {}", type(exc).__name__, exc)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc
    except Exception as exc:  # noqa: BLE001 —— 见下：不可信输入，仍按单条失败
        # 预期外的异常（多半是 ebooklib 在畸形输入上自己崩了）：照样逐条失败，
        # 但用 exception 级日志把栈打出来 —— 不要让一个 AttributeError 消失在
        # "不是有效的 EPUB 文件"后面，那会掩盖依赖 / 版本问题。
        logger.exception("读取 EPUB 时发生预期外异常：{}", type(exc).__name__)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc

    body = extract_body(book)
    if len(body) > MAX_BODY_CHARS:
        raise EpubReadError(EPUB_TOO_LARGE_ERROR)
    if not body:
        raise EpubReadError(EPUB_EMPTY_ERROR)

    return EpubContent(
        body=body,
        title=_first_metadata(book, "title"),
        author_name=_first_metadata(book, "creator", prefer_role="aut"),
    )


# ---------------------------------------------------------------------------
# 正文
# ---------------------------------------------------------------------------

def extract_body(book: epub.EpubBook) -> str:
    """按 spine 顺序把 XHTML 文档抽成纯文本（无正文返回空串）。"""
    chunks: list[str] = []
    for item in _spine_documents(book):
        content = item.get_content()
        if not content:
            continue
        text = html_to_text(content)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _spine_documents(book: epub.EpubBook):
    """spine 里的 XHTML 文档，跳过 nav 与封面页。

    spine 为空（畸形但可读的容器）时退回 manifest 里的全部 XHTML —— 宁可
    顺序差一点，也不要把一本读得出来的书判成"没有正文"。
    """
    seen: set[str] = set()
    for idref, _linear in book.spine or []:
        item = book.get_item_with_id(idref)
        if item is None or idref in seen:
            continue
        seen.add(idref)
        if _is_body_document(item):
            yield item

    if seen:
        return
    for item in book.get_items():
        if _is_body_document(item) and item.get_id() not in seen:
            seen.add(item.get_id())
            yield item


def _is_body_document(item) -> bool:
    """是不是要抽正文的 XHTML（nav / 封面 / 非文档一律不算）。

    规范要求正文文档是 ``application/xhtml+xml``，但现实里有 EPUB 错写成
    ``text/html``——那在 ebooklib 里落成普通 ``EpubItem`` 而不是 ``EpubHtml``。
    两种都收下；图片 / 样式 / 字体按 media_type 排除。``nav`` 文档里是目录链接
    而非正文，封面页同理。
    """
    if isinstance(item, (epub.EpubNav, epub.EpubCoverHtml)):
        return False
    if "nav" in (getattr(item, "properties", None) or []):
        return False
    if isinstance(item, epub.EpubHtml):
        return True
    return getattr(item, "media_type", "") in _BODY_MEDIA_TYPES


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
        # 自闭合标签只补一次换行（``<br/>`` 走默认实现会 start+end 各补一次）。
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
    """XHTML 字节 → 字符串：先用文件自己声明的编码，再走我们的候选编码。"""
    declared = _declared_encoding(data)
    if declared:
        try:
            return data.decode(declared)
        except (UnicodeError, LookupError):
            pass

    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        pass
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass
    for encoding in ("gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    # 结构性损坏的编码：宁可带替换符读出个大概，也不因为一段坏字节丢掉整本书。
    return data.decode("utf-8", errors="replace")


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

    lines = (
        _WS_RE.sub(" ", raw.replace("\xa0", " ")).strip()
        for raw in parser.text().split("\n")
    )
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# 元数据（标题 / 作者）
# ---------------------------------------------------------------------------

def _first_metadata(
    book: epub.EpubBook, name: str, *, prefer_role: str | None = None,
) -> str | None:
    """``dc:<name>`` 的第一个可用值；``prefer_role`` 命中时优先它。

    ``dc:creator`` 常见多个（作者 + 译者 + 校对），``opf:role="aut"`` 标出真正
    的作者；没有 role 属性时按 manifest 顺序取第一个。
    """
    candidates: list[tuple[str, dict]] = []
    for value, others in book.get_metadata("DC", name) or []:
        if not isinstance(value, str):
            continue
        cleaned = _clean_metadata(value)
        if cleaned:
            candidates.append((cleaned, others or {}))
    if not candidates:
        return None
    if prefer_role:
        for cleaned, others in candidates:
            if _role_of(others) == prefer_role:
                return cleaned
    return candidates[0][0]


def _role_of(others: dict) -> str:
    """``opf:role`` —— ebooklib 保留属性的命名空间前缀，逐个 key 取本地名比。"""
    for key, value in others.items():
        if key.rsplit("}", 1)[-1] == "role":
            return str(value).strip().lower()
    return ""


def _clean_metadata(value: str) -> str:
    """去标签 / 反转义 / 折叠空白 / 截断 —— 元数据是用户可控的脏输入。"""
    text = re.sub(r"<[^>]*>", " ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()
    return text[:MAX_METADATA_CHARS]
