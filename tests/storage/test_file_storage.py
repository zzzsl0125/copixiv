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


class TestDeleteNovelFiles:
    """删除一本小说 = 正文 + EPUB + **它自己的图片**，别人的一律不动。

    这条路径是销毁性的，而且 ``novel_path`` 来自数据库，所以两件事都要钉住：
    越界路径一个字节都不碰；删除要连插图一起回收（否则删完书，图还躺在树里
    等每周的孤儿扫描来收）。
    """

    def _tree(self, tmp_path):
        parent = tmp_path / "0000"
        parent.mkdir(parents=True)
        text = parent / "我的书_12345.txt"
        text.write_text("正文", encoding="utf-8")
        text.with_suffix(".epub").write_bytes(b"epub")
        for name in ("12345_u_1.jpg", "12345_p_2.png", "12345_c_cover.jpg",
                     "12346_u_9.jpg"):
            (parent / name).write_bytes(b"x")
        return parent, text

    def test_removes_text_epub_and_images_but_nothing_else(self, tmp_path):
        parent, text = self._tree(tmp_path)

        FileStorage(str(tmp_path)).delete_novel_files(str(text))

        assert sorted(p.name for p in parent.iterdir()) == ["12346_u_9.jpg"]

    def test_the_directory_is_removed_once_it_is_empty(self, tmp_path):
        parent, text = self._tree(tmp_path)
        (parent / "12346_u_9.jpg").unlink()

        FileStorage(str(tmp_path)).delete_novel_files(str(text))

        assert not parent.exists()

    def test_a_path_outside_the_root_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "重要文件_1.txt"
        victim.write_text("别删我", encoding="utf-8")

        FileStorage(str(tmp_path / "download")).delete_novel_files(str(victim))

        assert victim.exists()

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """文件早就没了 → 不抛异常；空目录顺带收掉（这是清理的既定行为）。"""
        parent = tmp_path / "0000"
        parent.mkdir()

        FileStorage(str(tmp_path)).delete_novel_files(
            str(parent / "不存在_1.txt")
        )

        assert not parent.exists()
