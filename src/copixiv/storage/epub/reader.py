"""EPUB 读取 —— 把 ``.epub`` 字节流读成入库管线要的正文 + 元数据。

职责只有一个：给定字节流，返回**正文**、EPUB 内嵌的**标题 / 作者**（可能
缺失）。不碰数据库、不碰磁盘、不做去重、不发号；那些都是
:mod:`copixiv.features.library.ingest` 的事。

放在 ``storage/epub/`` 与 :mod:`copixiv.storage.epub.builder`（**写**）对称：
格式机制归 storage，``features/library`` 只决定"这个上传该不该走这条读路径"。

**只用 ``zipfile`` + ``lxml``，不经过 ``ebooklib``。** 这条读路径要的东西很少
——OPF 里的 spine 顺序与两个 ``dc`` 字段，加每个 XHTML 的正文——而这四样
我们本来就得自己解析（编码自声明、块级标签、跳过 nav/封面）。走 ebooklib 时
还必须先把容器"瘦身"再喂给它（它会把每个 manifest 成员的字节都留在内存里，
一本 80 MB 的书能把进程吃掉），于是同一份 OPF 被解析两遍、失败分类被包一层、
版本差异还会以 ``AttributeError`` 的形式漏出来。现在只有一条路：

1. ``META-INF/container.xml`` → OPF 路径（:func:`_opf_path`）；
2. OPF → manifest（href / media-type / properties）+ spine 顺序；
3. 按 spine 顺序把每个正文文档抽成纯文本（:func:`html_to_text`）；
4. OPF → ``dc:title`` / ``dc:creator``（``opf:role="aut"`` 优先）。

内存跟着**正文**走，不跟容器里的插图走：非正文成员一个字节都不读。
"""

from __future__ import annotations

import codecs
import html
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, unquote

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

# 输入字节上限（压缩包本身）。上传是同步请求，所以这个上限是**容器**上限，
# 而不是"内存保护"——真正把内存压下来的是"只读正文成员"：真实语料里最大的
# 容器约 120 MiB（体积几乎全是插图），正文通常只有它的 8% 左右。
MAX_CONTAINER_BYTES = 128 * 1024 * 1024

# 解压后正文上限（字符）。它防的是"把一本书的正文撑爆 text 列 / FTS"，**不是**
# 容器大小保护（那由 MAX_CONTAINER_BYTES 负责）。
MAX_BODY_CHARS = 20_000_000

# 一次读取正文成员时最多拿多少字节。解压炸弹（一个声明很小的 zip 成员解出
# 几十 GB）在这里被截断，而不是把进程内存打满；超出部分反正也会触发
# MAX_BODY_CHARS。
_MAX_MEMBER_BYTES = 8 * 1024 * 1024

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

# 正文文档的 media_type：规范值 + 现实里常见的错写。
_BODY_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_XHTML_SUFFIXES = frozenset({".xhtml", ".html", ".htm"})

_WS_RE = re.compile(r"[ \t\f\v]+")

# OPF / container.xml 的命名空间。
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"


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
    if len(data) > MAX_CONTAINER_BYTES:
        raise EpubReadError(EPUB_TOO_LARGE_ERROR)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            package = _read_package(archive)
            body = _extract_body(archive, package)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, OSError) as exc:
        # 预期的读取失败：畸形 / 损坏 / 结构不对的容器。
        logger.warning("EPUB 读取失败：{}: {}", type(exc).__name__, exc)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc
    except etree.XMLSyntaxError as exc:
        logger.warning("EPUB 的 XML 解析失败：{}", exc)
        raise EpubReadError(EPUB_INVALID_ERROR) from exc
    except Exception as exc:  # noqa: BLE001 —— 不可信输入，仍按单条失败
        # 预期外的异常仍然逐条失败，但用 exception 级日志把栈打出来 ——
        # 不要让一个 AttributeError 消失在"不是有效的 EPUB 文件"后面。
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


# ---------------------------------------------------------------------------
# OPF：manifest + spine + dc 元数据
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Package:
    """一份 OPF 里我们真正要用的东西。

    ``spine`` 是**正文文档**的相对路径（相对 OPF 所在的目录，已 normpath），
    按书的顺序排列；``title`` / ``author_name`` 是清洗过的 ``dc`` 值。
    """

    spine: tuple[str, ...]
    title: str | None
    author_name: str | None


def _opf_path(container_xml: bytes) -> str:
    """``META-INF/container.xml`` → OPF 在 zip 里的路径（取不到返回空串）。

    优先 ``media-type="application/oebps-package+xml"`` 的那一个；没有就退回
    第一个 ``rootfile``（现实里有书错标 media-type，读得出来比严格重要）。
    """
    root = etree.fromstring(container_xml)
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
    """OPF 的相对 ``href`` → zip 成员名。

    带 ``#fragment`` 的只取路径部分；外部 URL 不算成员。
    """
    target = (href or "").split("#", 1)[0].strip()
    if not target or "://" in target:
        return ""
    return posixpath.normpath(posixpath.join(opf_dir, target))


def _manifest(
    opf_root, opf_dir: str,
) -> dict[str, tuple[str, str, frozenset[str]]]:
    """``id → (成员路径, media-type, properties)``。

    ``properties`` 用来认 nav / cover 文档（``properties="nav"``）。
    """
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


def _spine_paths(opf_root, manifest) -> tuple[str, ...]:
    """spine 顺序里的正文文档路径（跳过 nav / cover / 非正文）。

    spine 为空（畸形但可读的容器）时退回 manifest 里的全部 XHTML —— 宁可顺序
    差一点，也不要把一本读得出来的书判成"没有正文"。
    """
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
    """是不是要抽正文的 XHTML（nav / 封面 / 图片样式一律不算）。

    规范要求正文文档是 ``application/xhtml+xml``，但现实里有 EPUB 错写成别
    的；所以按"后缀像 XHTML **或** media-type 说是 HTML/XML 文档"收下，再排除
    nav / cover。``nav`` 文档里是目录链接而非正文，封面页同理。
    """
    if "nav" in props or "cover" in props:
        return False
    return (
        posixpath.splitext(path)[1].lower() in _XHTML_SUFFIXES
        or media_type in _BODY_MEDIA_TYPES
    )


def _first_dc(opf_root, name: str, *, prefer_role: str | None = None) -> str | None:
    """``dc:<name>`` 的第一个可用值；``prefer_role`` 命中时优先它。

    ``dc:creator`` 常见多个（作者 + 译者 + 校对），``opf:role="aut"`` 标出真正
    的作者；没有 role 属性时按 manifest 顺序取第一个。
    """
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


def _role_of(element) -> str:
    """``opf:role`` —— 属性名带命名空间，逐个 key 取本地名比。"""
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == "role":
            return str(value).strip().lower()
    return ""


def _clean_metadata(value: str) -> str:
    """去标签 / 反转义 / 折叠空白 / 截断 —— 元数据是用户可控的脏输入。"""
    text = re.sub(r"<[^>]*>", " ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()
    return text[:MAX_METADATA_CHARS]


def _read_package(archive: zipfile.ZipFile) -> _Package:
    """从已打开的容器里读出 OPF 的全部结论（manifest / spine / dc）。"""
    container_xml = archive.read("META-INF/container.xml")
    opf_path = _opf_path(container_xml)
    if not opf_path:
        raise EpubReadError(EPUB_INVALID_ERROR)
    opf_dir = posixpath.dirname(opf_path)
    opf_root = etree.fromstring(archive.read(opf_path))
    manifest = _manifest(opf_root, opf_dir)
    return _Package(
        spine=_spine_paths(opf_root, manifest),
        title=_first_dc(opf_root, "title"),
        author_name=_first_dc(opf_root, "creator", prefer_role="aut"),
    )


def _extract_body(archive: zipfile.ZipFile, package: _Package) -> str:
    """按 spine 顺序把正文成员抽成纯文本（无正文返回空串）。"""
    chunks: list[str] = []
    for path in package.spine:
        # spine 指向不存在的成员会抛 KeyError（→ 无效容器）：那是坏结构，
        # 不该悄悄少一章。声明里的百分号编码与归档里的裸写法两种都试 ——
        # ``%E4%B8%AD`` 与「中」指的是同一个成员。
        markup = _read_member(archive, path)
        text = html_to_text(markup)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _read_member(archive: zipfile.ZipFile, path: str) -> bytes:
    """读一个成员，最多 _MAX_MEMBER_BYTES（解压炸弹在这里被截断）。

    成员名在 OPF 里可能是百分号编码的，而在 zip 里是解码后的字面名（或反过来）
    —— 两种写法都试，第一次命中就用。都没有则抛 KeyError，由 :func:`read_epub`
    归类成无效容器。
    """
    candidates = (path, unquote(path), quote(path, safe="/"))
    for candidate in dict.fromkeys(candidates):
        try:
            with archive.open(candidate) as handle:
                return handle.read(_MAX_MEMBER_BYTES)
        except KeyError:
            continue
    raise KeyError(path)


# ---------------------------------------------------------------------------
# 正文
# ---------------------------------------------------------------------------

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
