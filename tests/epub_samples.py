"""测试用 EPUB 构造器 —— **不是**测试文件（pytest 不会收集它）。

`storage/epub/reader.py` 读的是真容器，所以测试也得喂真 EPUB：
用 ``ebooklib``（本就是运行依赖）现造一份字节流，比往仓库里塞二进制样例可控，
也比手工拼 zip 更接近用户手里那些文件的形状。

用法::

    data = build_epub(title="书名", author="作者", chapters=(("第一段。",),))
"""

from __future__ import annotations

import io

from ebooklib import epub


def build_epub(
    *,
    title: str | None = "测试书名",
    author: str | None = "测试作者",
    chapters: tuple[tuple[str, ...], ...] = (("第一段。", "第二段。"),),
    uid: str = "copixiv-test-epub",
    language: str = "zh",
    extra_body: str = "",
) -> bytes:
    """造一份最小可读 EPUB，返回字节流。

    * ``chapters``：一章一个元组，逐段渲染成 ``<p>``；章数即 spine 顺序；
    * ``title`` / ``author`` 传 ``None`` 即**不写**对应元数据（测回落用）；
    * ``extra_body``：追加到每章 ``<body>`` 里的原始 HTML（测实体 / 标签 / 跳过
      规则时用）。
    """
    book = epub.EpubBook()
    book.set_identifier(uid)
    if title is not None:
        book.set_title(title)
    book.set_language(language)
    if author is not None:
        book.add_author(author)

    items = []
    for index, paragraphs in enumerate(chapters, start=1):
        item = epub.EpubHtml(
            title=f"第{index}章", file_name=f"text/ch{index}.xhtml", lang=language,
        )
        body = "".join(f"<p>{text}</p>" for text in paragraphs) + extra_body
        item.content = (
            "<html><head><title>导航标题不该进正文</title></head>"
            f"<body>{body}</body></html>"
        )
        book.add_item(item)
        items.append(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = items
    book.spine = ["nav", *items]

    buffer = io.BytesIO()
    epub.write_epub(buffer, book)
    return buffer.getvalue()
