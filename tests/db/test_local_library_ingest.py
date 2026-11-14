"""Layer 2 DB tests: local library ingest (TXT/EPUB), SHA-256 dedup, negative axis monotonicity, and preset author."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from copixiv.core import formats
from copixiv.core.models import EpubStatus
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.library.ingest import (
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    ingest_uploads,
)
from copixiv.storage.file_storage import FileStorage


def _make_epub_bytes(title: str, creator: str, body_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>"
            f"</metadata><manifest>"
            f'<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            f"</manifest><spine><itemref idref=\"c1\"/></spine></package>",
        )
        zf.writestr("OEBPS/ch1.xhtml", f"<html><body><p>{body_text}</p></body></html>")
    return buf.getvalue()


def test_local_ingest_utf8_gb18030_epub_and_monotonic_negative_ids(
    uow: SqlUnitOfWork, tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    epub_bytes = _make_epub_bytes("EPUB标题", "EPUB作者", "第三本书的正文内容")

    with uow.begin():
        results = ingest_uploads(
            uow.session,
            storage,
            [
                ("《第一本书》作者：甲.txt", "第一本UTF8正文\r\n第二行".encode("utf-8")),
                ("第二本书.txt", "这是GB18030编码的中文小说正文内容，足够长以通过检测。".encode("gb18030")),
                ("ignored_name.epub", epub_bytes),
                ("bad.epub", b"not-a-zip"),
            ],
        )

        assert [r.status for r in results] == [
            STATUS_CREATED,
            STATUS_CREATED,
            STATUS_CREATED,
            STATUS_FAILED,
        ]
        created_ids = [r.novel_id for r in results[:3]]
        assert all(nid is not None and nid < 0 for nid in created_ids)
        # Negative coordinates strictly decrease (abs(id) monotonically increases)
        assert created_ids[0] > created_ids[1] > created_ids[2]

        n_txt = uow.session.get(models.Novel, created_ids[0])
        n_epub = uow.session.get(models.Novel, created_ids[2])
        assert n_txt is not None
        assert n_txt.source == "local"
        assert n_txt.has_epub == EpubStatus.NO_IMAGES
        assert Path(n_txt.path).is_file()

        assert n_epub is not None
        assert n_epub.title == "EPUB标题"
        assert n_epub.author_name == "EPUB作者"
        assert n_epub.has_epub == EpubStatus.DONE
        assert formats.sibling_path(Path(n_epub.path), formats.EPUB).is_file()


def test_local_ingest_dedup_attaches_epub_and_updates_preset_author(
    uow: SqlUnitOfWork, tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    body = "完全相同的规范化正文内容"

    with uow.begin():
        r1 = ingest_uploads(
            uow.session,
            storage,
            [("orig.txt", body.encode("utf-8"))],
        )
        assert r1[0].status == STATUS_CREATED
        nid = r1[0].novel_id
        assert nid is not None

    # Re-upload identical body as EPUB + preset_author_id=900
    epub_same_body = _make_epub_bytes("Orig", "Auth", body)
    with uow.begin():
        r2 = ingest_uploads(
            uow.session,
            storage,
            [("dup.epub", epub_same_body)],
            preset_author_id=900,
            preset_author_name="预设作者",
        )
        assert r2[0].status == STATUS_DUPLICATE
        assert r2[0].novel_id == nid
        assert r2[0].epub_attached is True

        novel = uow.session.get(models.Novel, nid)
        assert novel is not None
        assert novel.author_id == 900
        assert novel.author_name == "预设作者"
        assert novel.has_epub == EpubStatus.DONE
        assert formats.sibling_path(Path(novel.path), formats.EPUB).is_file()

        author = uow.session.get(models.Author, 900)
        assert author is not None
        assert author.novel_count == 1
        assert author.text == len(body)
