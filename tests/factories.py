"""Shared test builders for EPUB archives, local novel rows, and test FastAPI apps."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from copixiv.core.models import EpubStatus, new_shuffle_key


def make_minimal_epub(
    *,
    container_xml: bytes | None = None,
    opf_xml: bytes | None = None,
    chapters: dict[str, bytes] | None = None,
    mimetype: bytes = b"application/epub+zip",
) -> bytes:
    """Build an in-memory EPUB ZIP archive with configurable manifest/chapters."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", mimetype)
        zf.writestr(
            "META-INF/container.xml",
            container_xml
            or (
                b'<?xml version="1.0"?>'
                b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                b"<rootfiles>"
                b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                b"</rootfiles>"
                b"</container>"
            ),
        )
        zf.writestr(
            "OEBPS/content.opf",
            opf_xml
            or (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                b"<dc:title>Test Book</dc:title>"
                b"<dc:creator>Author X</dc:creator>"
                b"</metadata>"
                b"<manifest>"
                b'<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                b'<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
                b"</manifest>"
                b'<spine><itemref idref="c2"/><itemref idref="c1"/></spine>'
                b"</package>"
            ),
        )
        for name, content in (
            chapters
            or {
                "OEBPS/ch1.xhtml": b"<html><body><p>Second in spine</p></body></html>",
                "OEBPS/ch2.xhtml": b"<html><body><p>First in spine</p></body></html>",
            }
        ).items():
            zf.writestr(name, content)
    return buf.getvalue()


def make_simple_epub(title: str, creator: str, body_text: str) -> bytes:
    """Build a single-chapter EPUB archive for upload/ingest tests."""
    opf = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>"
        f"</metadata><manifest>"
        f'<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        f'</manifest><spine><itemref idref="c1"/></spine></package>'
    ).encode()
    return make_minimal_epub(
        opf_xml=opf,
        chapters={"OEBPS/ch1.xhtml": f"<html><body><p>{body_text}</p></body></html>".encode()},
    )


def write_epub_zip(path: Path, *, xhtml_body: str, with_image: bool) -> None:
    """Write a minimal EPUB container to *path* (for EPUB state/maintenance tests)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("EPUB/content.xhtml", xhtml_body.encode("utf-8"))
        if with_image:
            zf.writestr("EPUB/images/1.jpg", b"\xff\xd8\xff\xe0img")


def make_local_novel(
    novel_id: int = -1,
    *,
    title: str = "Local Book",
    author_id: int | None = 1,
    author_name: str | None = "Author",
    path: str | None = None,
    source_key: str | None = None,
    has_epub: int = EpubStatus.NO_IMAGES,
    **kwargs,
):
    """Build a ``models.Novel`` ORM row representing a locally uploaded book."""
    from copixiv.db import models

    return models.Novel(
        id=novel_id,
        title=title,
        author_id=author_id,
        author_name=author_name,
        path=path or f"download/local/{title}_{novel_id}.txt",
        like=kwargs.pop("like", 0),
        view=kwargs.pop("view", 0),
        text=kwargs.pop("text", 100),
        has_epub=has_epub,
        shuffle=kwargs.pop("shuffle", new_shuffle_key()),
        tags=kwargs.pop("tags", []),
        source="local",
        source_key=source_key or f"sha_local_{abs(novel_id)}",
        **kwargs,
    )


def make_test_app(
    session_factory,
    tmp_path: Path,
    *,
    api_key: str = "",
):
    """Build an isolated FastAPI app with temp download/backup/log directories."""
    from copixiv.app import create_app
    from copixiv.config import AppConfig, BackupConfig, PathConfig, SecurityConfig

    cfg = AppConfig(
        path=PathConfig(download=str(tmp_path / "download")),
        backup=BackupConfig(dir=str(tmp_path / "backups")),
        security=SecurityConfig(api_key=api_key, allowed_hosts=["localhost"]),
    )
    return create_app(
        config=cfg,
        log_dir=str(tmp_path / "logs"),
        session_factory=session_factory,
        skip_init_db=True,
        skip_auto_backup=True,
        mount_spa=False,
        run_background_tasks=False,
    )
