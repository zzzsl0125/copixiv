"""Layer 1 unit tests: filename safety, NAME_MAX truncation, naming templates, and asset conventions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from copixiv.core import naming as formats
from copixiv.core.naming import NovelNamingTemplate, build_path, safe_filename
from copixiv.storage import assets
from copixiv.storage.file_storage import FileStorage


def test_naming_template_requires_id():
    with pytest.raises(ValueError, match=r"\{id\}"):
        NovelNamingTemplate("{author_name}/{title}")


def test_naming_template_sanitizes_illegal_chars_and_reserved_names():
    tpl = NovelNamingTemplate("{author_name}/{series_name}/#{series_index}_{title}_{id}")
    novel = SimpleNamespace(
        id=42,
        title='A/B:C*D?E<F>G|H\\I"J\x00',
        author_name="CON",
        series_name="..NUL",
        series_index=0,
        like=0,
        view=10,
        text=0,
        create_time=datetime(2026, 5, 1, tzinfo=UTC),
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
    # Worst-case sibling EPUB filename must fit within NAME_MAX (255 bytes)
    epub_name = formats.sibling_path(built, formats.EPUB).name
    assert len(epub_name.encode("utf-8")) <= 255
    assert built.name.endswith(f"_{novel_id}.txt")

    # Small IDs go into 0000 shard
    small_built = Path(build_path(123, "Title", download_dir="dl"))
    assert small_built.parent == Path("dl/0000")

    # Naming template clamps dir segments <=255 UTF-8 bytes and leaf <=250 UTF-8 bytes preserving _{id}
    long_tpl = NovelNamingTemplate("{author_name}/{title}_{id}")
    overlong = SimpleNamespace(
        id=999,
        title="星" * 120,
        author_name="甲" * 120,
        series_name=None,
        series_index=None,
        like=0,
        view=0,
        text=0,
        create_time=None,
    )
    resolved = long_tpl.resolve(overlong)
    dir_part, leaf_part = resolved.split("/")
    assert len(dir_part.encode("utf-8")) <= 255
    assert len(leaf_part.encode("utf-8")) <= 250
    assert leaf_part.endswith("_999")
    assert len((leaf_part + ".epub").encode("utf-8")) <= 255


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


def test_naming_prevents_secondary_token_expansion_and_has_no_raw_bidi_chars():
    tpl = NovelNamingTemplate("{title}_{author_name}_{series_name}_{id}")
    novel = SimpleNamespace(
        id=7,
        title="Book {author_name} {series_name}",
        author_name="Auth{id}",
        series_name="Ser",
        series_index=1,
        like=0,
        view=0,
        text=0,
        create_time=None,
    )
    assert tpl.resolve(novel) == "Book {author_name} {series_name}_Auth{id}_Ser_7"

    naming_src = Path("src/copixiv/core/naming.py").read_text(encoding="utf-8")
    raw_bidi = {chr(cp) for r in (range(0x202A, 0x202F), range(0x2066, 0x206A), (0x200E, 0x200F)) for cp in r}
    assert not any(ch in naming_src for ch in raw_bidi)


def test_assets_case_insensitive_int_dedup_tmp_filter_and_file_storage_guards(tmp_path: Path):
    # int/str dedup + uppercase extension + .tmp exclusion
    upper_cover = tmp_path / "777_c_cover.JPG"
    upper_img = tmp_path / "777_u_42.PNG"
    tmp_leftover = tmp_path / "777_u_99.jpg.tmp"
    for p in (upper_cover, upper_img, tmp_leftover):
        p.write_bytes(b"data")

    assert assets.asset_suffix("https://i.pximg.net/img/a.PNG?x=1") == ".png"
    assert assets.find_cover(tmp_path, 777) == upper_cover
    assert assets.iter_assets(
        tmp_path,
        777,
        [(assets.UPLOADED, 42), (assets.ILLUSTRATION, "42")],
    ) == {"42": upper_img}
    assert set(assets.iter_novel_asset_files(tmp_path, 777)) == {upper_cover, upper_img}

    only_tmp_dir = tmp_path / "only_tmp"
    only_tmp_dir.mkdir()
    (only_tmp_dir / "888_u_1.jpg.tmp").write_bytes(b"partial")
    assert assets.has_illustration_assets(only_tmp_dir, 888) is False

    dl_root = tmp_path / "dl"
    dl_root.mkdir()
    storage = FileStorage(str(dl_root))
    # 255-byte .txt fits NAME_MAX on its own, but sibling .epub is 256 bytes -> must be rejected
    prefix = "a" * (255 - len("_1.txt"))
    edge_txt = dl_root / "0000" / f"{prefix}_1.txt"
    assert storage.adopt_anchor(str(edge_txt)) is None
    # Root itself must never be deleted
    storage.delete_novel_files(str(dl_root))
    assert dl_root.is_dir()

    # atomic_write uses a short random temp path so exact 255-byte targets succeed cleanly
    from concurrent.futures import ThreadPoolExecutor

    from copixiv.storage.atomic import atomic_write, write_text_atomic

    max_name = "b" * (255 - len(".txt")) + ".txt"
    max_target = dl_root / max_name
    write_text_atomic(max_target, "hello-255", root=dl_root)
    assert max_target.read_text(encoding="utf-8") == "hello-255"

    # Concurrent atomic_write on the same path leaves no stray .tmp files
    shared_target = dl_root / "concurrent.txt"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: write_text_atomic(shared_target, f"v{i}", root=dl_root), range(8)))
    assert list(dl_root.glob("*.tmp")) == []

    # Failed atomic_write cleans up temp file and blocks root escape
    def _boom_write() -> None:
        with atomic_write(dl_root / "fail.txt", root=dl_root) as tmp_f:
            tmp_f.write_bytes(b"partial")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _boom_write()
    assert list(dl_root.glob("*.tmp")) == []
    with pytest.raises(OSError, match="download 根之外"):
        write_text_atomic(tmp_path / "outside.txt", "nope", root=dl_root)
