"""文件格式约定 —— 落盘后缀 / HTTP media type / "导出取哪种" 的单源。

放在 ``core/`` 而不是 ``storage/``：这是**领域词汇**（本仓认哪两种书、导出时
取哪一种），不是 I/O 机制。``core/services.build_batch_zip`` 与 ``features/``
都要用它，而 ``core`` 从不反向依赖 ``storage``（分层契约）。

立这个模块是因为同一套约定曾经散在多处：``".epub"`` 字面量出现在
``file_storage`` / ``epub.builder`` / ``image_downloader`` / ``maintenance``
四个文件里，而"``prefer_epub`` 且 EPUB 就绪才取 epub"这条判定在
``core/services.build_batch_zip`` 与 ``features/novels/batch_download``
里逐字重复了一遍。约定漂移一次，导出就会拿到不存在的文件。

读路径的核心不变式（§3.2）：**``novel.path`` 永远指向 ``.txt``**，EPUB 是它
同基名的兄弟文件。所以"换格式"= 换后缀，:func:`sibling_path` 就是那个动作。
"""

from __future__ import annotations

from pathlib import Path

TEXT = "txt"
EPUB = "epub"

SUFFIXES: dict[str, str] = {TEXT: ".txt", EPUB: ".epub"}
MEDIA_TYPES: dict[str, str] = {TEXT: "text/plain", EPUB: "application/epub+zip"}
# 删除 / 清理时要一并处理的后缀，顺序即处理顺序。
ALL_SUFFIXES: tuple[str, ...] = (SUFFIXES[TEXT], SUFFIXES[EPUB])


def suffix_for(kind: str) -> str:
    """``"txt"`` → ``".txt"``。"""
    return SUFFIXES[kind]


def sibling_path(path: Path, kind: str) -> Path:
    """同一本书换一种格式的路径：``我的书_-1.txt`` ↔ ``我的书_-1.epub``。"""
    return path.with_suffix(SUFFIXES[kind])


def media_type_for(kind: str) -> str:
    """该格式的 HTTP ``Content-Type``（下载端点用）。"""
    return MEDIA_TYPES[kind]


def preferred_format(format_mode: str, *, epub_ready: bool) -> str:
    """导出 / 预览取哪种格式。

    ``prefer_epub`` 且 EPUB 确实就绪（``has_epub == DONE``）才用 epub，
    否则一律 txt —— 取一个不存在的文件比降级成 txt 糟得多。
    """
    return EPUB if (format_mode == "prefer_epub" and epub_ready) else TEXT
