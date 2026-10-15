"""文件格式约定单测 —— ``core/formats.py``。

权威：``docs/LIBRARY_DESIGN.md`` §3.2（后缀约定）/ §4.1（EPUB）。
这里钉的是"单源"本身：后缀、media type、以及导出取哪种格式的真值表 ——
后者原先在 ``core/services.build_batch_zip`` 与 ``batch_download`` 里
各写了一遍，本文件保证它只剩一个答案。
"""

from __future__ import annotations

from pathlib import Path

from copixiv.core import formats


class TestVocabulary:
    def test_suffix_and_media_type(self):
        assert formats.suffix_for(formats.TEXT) == ".txt"
        assert formats.suffix_for(formats.EPUB) == ".epub"
        assert formats.media_type_for(formats.TEXT) == "text/plain"
        assert formats.media_type_for(formats.EPUB) == "application/epub+zip"

    def test_sibling_path_swaps_the_suffix(self):
        assert formats.sibling_path(Path("a/书_-1.txt"), formats.EPUB) == (
            Path("a/书_-1.epub")
        )
        assert formats.sibling_path(Path("a/书_-1.epub"), formats.TEXT) == (
            Path("a/书_-1.txt")
        )

    def test_all_suffixes_covers_both(self):
        assert set(formats.ALL_SUFFIXES) == {".txt", ".epub"}


class TestPreferredFormat:
    def test_prefer_epub_still_needs_a_ready_file(self):
        assert formats.preferred_format("prefer_epub", epub_ready=True) == "epub"
        # 取一个不存在的文件比降级成 txt 糟得多。
        assert formats.preferred_format("prefer_epub", epub_ready=False) == "txt"

    def test_txt_mode_never_picks_epub(self):
        assert formats.preferred_format("txt", epub_ready=True) == "txt"
        assert formats.preferred_format("txt", epub_ready=False) == "txt"
