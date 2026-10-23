"""``FileStorage`` — the anchor rules that decide where a body may be written."""

from __future__ import annotations

from pathlib import Path

from copixiv.storage.file_storage import FileStorage


class TestAdoptAnchor:
    """A stored ``novel.path`` is a write target only if it is actually writable.

    ``adopt_anchor`` exists because ``novel.path`` comes from the database: it is
    trusted only after the same containment rule ``delete_novel_files`` applies.
    """

    def test_inside_root_is_adopted(self, tmp_path):
        storage = FileStorage(str(tmp_path))
        anchor = str(tmp_path / "0000" / "短标题_123.txt")
        assert storage.adopt_anchor(anchor) == anchor

    def test_outside_root_is_refused(self, tmp_path):
        storage = FileStorage(str(tmp_path))
        assert storage.adopt_anchor("/etc/passwd") is None

    def test_none_and_empty_are_refused(self, tmp_path):
        storage = FileStorage(str(tmp_path))
        assert storage.adopt_anchor(None) is None
        assert storage.adopt_anchor("") is None

    def test_name_too_long_for_the_tmp_sibling_is_refused(self, tmp_path):
        """A stored path must leave room for the atomic write's ``.tmp``.

        Regression for three production rows (27001932 / 27038159 / 28571161):
        paths written before ``build_path`` reserved the ``.tmp`` suffix sit at
        253 bytes, so ``x.txt`` fits but ``x.txt.tmp`` needs 257.  The atomic
        write appends the suffix, so every download through such an anchor died
        with ``OSError: [Errno 36] File name too long`` — and the re-download
        could never succeed while the old name was reused.
        """
        storage = FileStorage(str(tmp_path))
        # 82 CJK glyphs (3 bytes each) + "_1.txt" = 252 bytes of basename.
        # ``x.txt`` fits NAME_MAX on its own; ``x.txt.tmp`` (256) does not —
        # exactly the production shape (stored paths at 252-253 bytes).
        name = "长" * 82
        anchor = str(tmp_path / "0000" / f"{name}_1.txt")

        assert len(Path(anchor).name.encode()) == 252
        assert len(Path(anchor).name.encode()) + len(".tmp") > 255
        assert storage.adopt_anchor(anchor) is None, (
            "252B + '.tmp' = 256B > NAME_MAX, must be refused"
        )

    def test_a_name_that_exactly_fits_is_adopted(self, tmp_path):
        storage = FileStorage(str(tmp_path))
        # 81 glyphs (249 bytes) leaves room for the four extra bytes of ".tmp".
        name = "长" * 81
        anchor = str(tmp_path / "0000" / f"{name}_1.txt")

        assert len(Path(anchor).name.encode()) + len(".tmp") <= 255
        assert storage.adopt_anchor(anchor) == anchor

    def test_name_max_is_measured_in_bytes_not_characters(self, tmp_path):
        """CJK is three bytes per glyph; a 90-character name is ~270 bytes."""
        storage = FileStorage(str(tmp_path))
        ninety = "あ" * 90
        anchor = str(tmp_path / "0000" / f"{ninety}.txt")

        assert len(ninety) < 255            # fine by character count
        assert len(Path(anchor).name.encode()) > 255
        assert storage.adopt_anchor(anchor) is None
