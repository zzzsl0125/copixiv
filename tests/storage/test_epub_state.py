"""EPUB file state predicates — the single owner (`storage/epub/state.py`).

Three copies of the marker regex used to live in the builder, the downloader
and the reconciler; these tests pin the *shared* facts they now delegate to,
so a future "just inline it here" cannot silently re-fork the semantics.

Also covers malformed containers: the reader must classify them as an
``EpubReadError`` and **never** crash, while the state predicates must answer
"readable / complete" without raising on garbage.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from copixiv.core.services import (
    has_image_placeholders,
    has_image_placeholders_bytes,
)
from copixiv.storage.epub import state
from copixiv.storage.epub.reader import (
    EPUB_EMPTY_ERROR,
    EPUB_INVALID_ERROR,
    EpubReadError,
    read_epub,
)
from tests.epub_samples import build_epub


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _zip_of(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


class TestMarkerSyntaxHasOneOwner:
    """Text and bytes entry points must accept exactly the same marker set."""

    @pytest.mark.parametrize("marker", [
        "[uploadedimage:12345]",
        "[pixivimage:67890]",
        "[pixivimage:67890-1]",
    ])
    def test_both_entry_points_agree(self, marker):
        assert has_image_placeholders(marker) is True
        assert has_image_placeholders_bytes(marker.encode()) is True

    @pytest.mark.parametrize("text", [
        "plain text",
        "[uploadedimage:]",
        "[other:123]",
        "[pixivimage:abc]",
        "",
    ])
    def test_both_entry_points_reject_the_same_non_markers(self, text):
        assert has_image_placeholders(text) is False
        assert has_image_placeholders_bytes(text.encode()) is False

    def test_none_and_empty_bytes(self):
        assert has_image_placeholders(None) is False
        assert has_image_placeholders_bytes(None) is False
        assert has_image_placeholders_bytes(b"") is False


class TestFilePredicates:
    def test_missing_and_non_zip_are_not_readable(self, tmp_path):
        missing = tmp_path / "nope.epub"
        assert state.is_readable_container(missing) is False

        garbage = _write(tmp_path / "bad.epub", b"not a zip")
        assert state.is_readable_container(garbage) is False
        # Predicates must degrade, not raise, on unreadable input.
        assert state.has_raw_markers(garbage) is False
        assert state.has_embedded_images(garbage) is False
        assert state.is_complete(garbage) is False

    def test_a_bare_zip_is_readable_but_not_complete(self, tmp_path):
        """Zip-ness answers "can it open", never "is the book finished"."""
        path = _write(
            tmp_path / "shell.epub",
            _zip_of(("mimetype", b"application/epub+zip"),
                    ("content.xhtml", b"<html><body>text</body></html>")),
        )
        assert state.is_readable_container(path) is True
        assert state.has_raw_markers(path) is False
        assert state.has_embedded_images(path) is False
        assert state.is_complete(path) is False

    def test_raw_marker_is_detected_and_blocks_completeness(self, tmp_path):
        path = _write(
            tmp_path / "marked.epub",
            _zip_of(
                ("images/1.jpg", b"\xff\xd8\xff"),
                ("content.xhtml", b"<p>[uploadedimage:12345]</p>"),
            ),
        )
        assert state.has_embedded_images(path) is True
        assert state.has_raw_markers(path) is True
        assert state.is_complete(path) is False

    def test_only_uppercase_xhtml_is_scanned(self, tmp_path):
        """Real EPUBs ship ``.XHTML``; the suffix match is case-insensitive."""
        path = _write(
            tmp_path / "upper.epub",
            _zip_of(("images/1.jpg", b"x"),
                    ("CH1.XHTML", b"<p>[pixivimage:9]</p>")),
        )
        assert state.has_raw_markers(path) is True

    def test_cover_alone_does_not_count_as_embedded_images(self, tmp_path):
        """A cover-only EPUB must not pass as "has illustrations"."""
        path = _write(
            tmp_path / "cover.epub",
            _zip_of(("cover.jpg", b"\xff\xd8\xff"),
                    ("content.xhtml", b"<p>no markers</p>")),
        )
        assert state.has_embedded_images(path) is False
        assert state.is_complete(path) is False

    def test_complete_book_passes(self, tmp_path):
        path = _write(
            tmp_path / "done.epub",
            _zip_of(("images/1.jpg", b"\xff\xd8\xff"),
                    ("content.xhtml", b"<p>resolved</p>")),
        )
        assert state.is_readable_container(path) is True
        assert state.is_complete(path) is True


class TestSingleOpen:
    """``is_complete`` must ask both questions of **one** archive handle."""

    def test_is_complete_opens_the_container_once(self, tmp_path, monkeypatch):
        path = _write(
            tmp_path / "done.epub",
            _zip_of(("images/1.jpg", b"\xff\xd8\xff"),
                    ("content.xhtml", b"<p>resolved</p>")),
        )
        openings: list[object] = []
        original = zipfile.ZipFile

        class Counting(original):
            def __init__(self, *args, **kwargs):
                openings.append(args[0] if args else kwargs.get("file"))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(zipfile, "ZipFile", Counting)
        assert state.is_complete(path) is True
        assert len(openings) == 1

    def test_is_complete_agrees_with_the_two_predicates(self, tmp_path):
        """The combined answer is exactly ``images and not markers``."""
        cases = {
            "complete": (("images/1.jpg", b"x"),
                         ("content.xhtml", b"<p>ok</p>")),
            "no_images": (("content.xhtml", b"<p>ok</p>"),),
            "marker": (("images/1.jpg", b"x"),
                       ("content.xhtml", b"<p>[uploadedimage:1]</p>")),
            "both": (("content.xhtml", b"<p>[pixivimage:2]</p>"),),
        }
        for name, members in cases.items():
            path = _write(tmp_path / f"{name}.epub", _zip_of(*members))
            expected = (
                state.has_embedded_images(path)
                and not state.has_raw_markers(path)
            )
            assert state.is_complete(path) is expected, name

    def test_unreadable_member_does_not_raise(self, tmp_path):
        """A zip whose member read fails must degrade, not crash."""
        path = _write(
            tmp_path / "broken.epub",
            _zip_of(("images/1.jpg", b"x"), ("content.xhtml", b"ok")),
        )
        # Truncate so the central directory survives but member data does not.
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        assert state.is_complete(path) in (True, False)   # no exception
        assert state.has_raw_markers(path) in (True, False)


class TestNeedsEpub:
    def test_unknown_body_is_none(self):
        assert state.needs_epub(None) is None

    def test_plain_body_is_false(self):
        assert state.needs_epub("纯文字正文") is False

    def test_marker_body_is_true(self):
        assert state.needs_epub("正文 [uploadedimage:1]") is True


class TestMalformedContainers:
    """Every malformed input must become a classified ``EpubReadError``.

    The reader's contract is "one bad upload = one failed item"; an
    ``AttributeError`` escaping from a library on a weird container would
    abort the whole batch instead.
    """

    @pytest.mark.parametrize("data,expected", [
        # No mimetype member at all.
        (_zip_of(("content.xhtml", b"<html><body><p>x</p></body></html>")),
         EPUB_INVALID_ERROR),
        # Empty zip.
        (_zip_of(), EPUB_INVALID_ERROR),
        # Truncated zip (the central directory is cut off).
        (build_epub()[:40], EPUB_INVALID_ERROR),
        # A zip that is not an EPUB at all.
        (_zip_of(("readme.txt", b"hello")), EPUB_INVALID_ERROR),
        # Broken OPF: the manifest references a file that does not exist.
        (_zip_of(
            ("mimetype", b"application/epub+zip"),
            ("META-INF/container.xml", (
                '<?xml version="1.0"?><container><rootfiles><rootfile '
                'full-path="content.opf" media-type="application/oebps-package+xml"/>'
                '</rootfiles></container>'
            ).encode()),
            ("content.opf", b"<package><manifest><item href='missing.xhtml'/></manifest></package>"),
        ), EPUB_INVALID_ERROR),
        # Empty XHTML: readable container, nothing to extract.
        (build_epub(chapters=(("",),)), EPUB_EMPTY_ERROR),
    ])
    def test_classified_not_crashing(self, data, expected):
        with pytest.raises(EpubReadError) as excinfo:
            read_epub(data)
        assert str(excinfo.value) == expected

    def test_encrypted_container_still_opens_or_fails_cleanly(self):
        """An ``encryption.xml``-bearing container must not crash the batch.

        ebooklib does not decrypt, so either it yields no body (→
        ``EPUB_EMPTY_ERROR``) or it rejects the container (→
        ``EPUB_INVALID_ERROR``); both are per-item failures. What is forbidden
        is an unclassified exception.
        """
        data = _zip_of(
            ("mimetype", b"application/epub+zip"),
            ("META-INF/container.xml", (
                '<?xml version="1.0"?><container><rootfiles><rootfile '
                'full-path="content.opf" media-type="application/oebps-package+xml"/>'
                '</rootfiles></container>'
            ).encode()),
            ("META-INF/encryption.xml", (
                '<?xml version="1.0"?><encryption '
                'xmlns="http://www.w3.org/2001/04/xmlenc#">'
                '<EncryptedData><CipherData><CipherValue>AA==</CipherValue>'
                '</CipherData></EncryptedData></encryption>'
            ).encode()),
            ("content.opf", (
                '<?xml version="1.0"?><package '
                'xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:title>t</dc:title></metadata>'
                '<manifest><item id="c1" href="c1.xhtml" '
                'media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="c1"/></spine></package>'
            ).encode()),
            ("c1.xhtml", b"<html><body><p>encrypted?</p></body></html>"),
        )
        try:
            content = read_epub(data)
        except EpubReadError as exc:
            assert str(exc) in {EPUB_INVALID_ERROR, EPUB_EMPTY_ERROR}
        else:
            assert isinstance(content.body, str)

    def test_malformed_bytes_never_raise_anything_else(self):
        """Fuzz-ish sweep: arbitrary byte soup must only ever raise EpubReadError."""
        samples = [
            b"PK\x03\x04" + b"\x00" * 20,
            b"PK\x05\x06" + b"\x00" * 18,          # empty-zip signature only
            b"<html><body>not a zip</body></html>",
            b"\xff\xfe\x00\x00garbage",
            build_epub()[:200],
        ]
        for data in samples:
            try:
                read_epub(data)
            except EpubReadError:
                pass
            except Exception as exc:  # pragma: no cover - the assertion is the point
                pytest.fail(
                    f"unclassified {type(exc).__name__} escaped read_epub: {exc}"
                )
