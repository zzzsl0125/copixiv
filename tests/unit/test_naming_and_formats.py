"""Layer 1 unit tests: filename safety, NAME_MAX truncation, naming templates, and asset conventions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from copixiv.core import formats
from copixiv.core.naming import NovelNamingTemplate, build_path, safe_filename
from copixiv.storage import assets


def test_naming_template_requires_id():
    with pytest.raises(ValueError, match=r"\{id\}"):
        NovelNamingTemplate("{author_name}/{title}")


def test_naming_template_sanitizes_illegal_chars_and_reserved_names():
    tpl = NovelNamingTemplate("{author_name}/{series_name}/#{series_index}_{title}_{id}")
    novel = SimpleNamespace(
        id=42,
        title="A/B:C*D?E<F>G|H\\I\"J\x00",
        author_name="CON",
        series_name="..NUL",
        series_index=0,
        like=0,
        view=10,
        text=0,
        create_time=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    resolved = tpl.resolve(novel)
    parts = resolved.split("/")
    assert parts[0] == "CON[WinReserved]"
    assert parts[1] == "NUL[WinReserved]"
    # series_index == 0 collapses "#{series_index}_"
    assert not parts[2].startswith("#")
    assert "/" not in parts[2]
    assert "\x00" not in parts[2]
    assert parts[2].endswith("_42")


def test_naming_template_keeps_zero_for_numeric_stats_and_author_fallback():
    tpl = NovelNamingTemplate("{author_name}/{like}_{text}_{date}_{id}")
    novel = SimpleNamespace(
        id=9,
        title="T",
        author_name=None,
        like=0,
        view=0,
        text=0,
        create_time="2026-10-01T12:00:00+09:00",
    )
    assert tpl.resolve(novel) == "未知作者/0_0_2026-10-01_9"


def test_safe_filename_and_build_path_respect_name_max_utf8():
    assert safe_filename('///\\:*?"<>|') == "untitled"

    long_cjk_title = "星辰大海的彼端" * 40  # > 800 UTF-8 bytes
    novel_id = 123456789
    built = Path(build_path(novel_id, long_cjk_title, download_dir="dl"))
    assert built.parent == Path("dl/1234")
    # Worst-case temp EPUB file name during atomic write must fit in 255 bytes
    tmp_epub_name = formats.sibling_path(built, formats.EPUB).name + ".tmp"
    assert len(tmp_epub_name.encode("utf-8")) <= 255
    assert built.name.endswith(f"_{novel_id}.txt")

    # Small IDs go into 0000 shard
    small_built = Path(build_path(123, "Title", download_dir="dl"))
    assert small_built.parent == Path("dl/0000")


def test_formats_and_assets_conventions(tmp_path: Path):
    txt = tmp_path / "book_-10.txt"
    epub = formats.sibling_path(txt, formats.EPUB)
    assert epub == tmp_path / "book_-10.epub"
    assert formats.preferred_format("prefer_epub", epub_ready=True) == formats.EPUB
    assert formats.preferred_format("prefer_epub", epub_ready=False) == formats.TEXT
    assert formats.preferred_format("txt", epub_ready=True) == formats.TEXT

    cover = assets.asset_path(tmp_path, 555, assets.COVER, suffix=".png")
    u1 = assets.asset_path(tmp_path, 555, assets.UPLOADED, "img1", suffix=".jpg")
    p1 = assets.asset_path(tmp_path, 555, assets.ILLUSTRATION, "ill9", suffix=".gif")
    for p in (cover, u1, p1):
        p.write_bytes(b"x")

    assert assets.find_cover(tmp_path, 555) == cover
    assert assets.novel_id_of(cover.stem) == 555
    assert assets.novel_id_of(u1.stem) == 555
    assert assets.novel_id_of(p1.stem) == 555
    assert assets.novel_id_of("random_file") is None
    assert assets.has_illustration_assets(tmp_path, 555) is True
    assert assets.iter_assets(tmp_path, 555, [(assets.UPLOADED, "img1")]) == {"img1": u1}
    assert assets.scan_assets(tmp_path, 555) == {"img1": u1, "ill9": p1}
    assert set(assets.iter_novel_asset_files(tmp_path, 555)) == {cover, u1, p1}
