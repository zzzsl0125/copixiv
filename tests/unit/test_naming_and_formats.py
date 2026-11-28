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

    # Aware UTC datetime converts to JST before formatting {date}
    jst_rollover = SimpleNamespace(
        id=10,
        title="T",
        author_name="A",
        like=1,
        view=1,
        text=1,
        create_time=datetime(2026, 5, 1, 15, 0, tzinfo=UTC),
    )
    assert tpl.resolve(jst_rollover) == "A/1_1_2026-05-02_10"
    same_jst_day = SimpleNamespace(
        id=11,
        title="T",
        author_name="A",
        like=1,
        view=1,
        text=1,
        create_time=datetime(2026, 5, 1, 14, 59, tzinfo=UTC),
    )
    assert tpl.resolve(same_jst_day) == "A/1_1_2026-05-01_11"


def test_safe_filename_and_build_path_respect_name_max_utf8():
    assert safe_filename('///\\:*?"<>|') == "untitled"

    long_cjk_title = "星辰大海的彼端" * 40  # > 800 UTF-8 bytes
    novel_id = 123456789
    built = Path(build_path(novel_id, long_cjk_title, download_dir="dl"))
    assert built.parent == Path("dl/pixiv/1234")
    # Worst-case sibling EPUB filename must fit within NAME_MAX (255 bytes)
    epub_name = formats.sibling_path(built, formats.EPUB).name
    assert len(epub_name.encode("utf-8")) <= 255
    assert built.name.endswith(f"_{novel_id}.txt")

    # Small IDs go into pixiv/0000 shard
    small_built = Path(build_path(123, "Title", download_dir="dl"))
    assert small_built.parent == Path("dl/pixiv/0000")

    # Negative local IDs go into local/ shard and match FileStorage.local_novel_path
    from copixiv.storage.file_storage import FileStorage

    local_built = Path(build_path(-123, "标题", download_dir="dl"))
    assert local_built == Path("dl/local/标题_-123.txt")
    assert local_built == FileStorage("dl").local_novel_path(-123, "标题")
    assert Path(build_path(-123, "标题", download_dir="dl", kind="epub")) == FileStorage(
        "dl"
    ).local_novel_path(-123, "标题", "epub")

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


def test_assets_int_dedup_tmp_filter_and_file_storage_guards(tmp_path: Path):
    # int/str dedup + .tmp exclusion + case-insensitive iter_novel_asset_files
    cover = tmp_path / "777_c_cover.jpg"
    img = tmp_path / "777_u_42.png"
    upper_img = tmp_path / "777_p_43.PNG"
    tmp_leftover = tmp_path / "777_u_99.jpg.tmp"
    for p in (cover, img, upper_img, tmp_leftover):
        p.write_bytes(b"data")

    assert assets.asset_suffix("https://i.pximg.net/img/a.PNG?x=1") == ".png"
    assert assets.find_cover(tmp_path, 777) == cover
    assert assets.iter_assets(
        tmp_path,
        777,
        [(assets.UPLOADED, 42), (assets.ILLUSTRATION, "42")],
    ) == {"42": img}
    assert set(assets.iter_novel_asset_files(tmp_path, 777)) == {cover, img, upper_img}

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


def test_build_path_multi_source_and_naming_template_source(tmp_path: Path):
    # Explicit external source routes to {download_dir}/{source}/
    esj_path = Path(build_path(42, "ESJ书名", download_dir="dl", source="esj"))
    assert esj_path == Path("dl/esj/ESJ书名_42.txt")

    # Explicit local source routes to local/
    local_path = Path(build_path(-5, "Local书名", download_dir="dl", source="local"))
    assert local_path == Path("dl/local/Local书名_-5.txt")

    # source=None routes negative IDs to local/, positive to pixiv/0000/; source="pixiv" routes to pixiv/
    assert Path(build_path(-5, "T", download_dir="dl", source=None)).parent == Path("dl/local")
    assert Path(build_path(-5, "T", download_dir="dl", source="pixiv")).parent == Path("dl/pixiv/0000")
    for src in (None, "pixiv"):
        assert Path(build_path(123, "T", download_dir="dl", source=src)).parent == Path("dl/pixiv/0000")
        assert Path(build_path(12345678, "T", download_dir="dl", source=src)).parent == Path("dl/pixiv/1234")

    # Multi-source series layout: {download_dir}/{source}/{series}_{key}/{0001}_{title}_{id}.ext
    pixiv_series = Path(
        build_path(
            999,
            "序章",
            download_dir="dl",
            source="pixiv",
            series_id=777,
            series_name="魔法纪元",
            series_index=1,
        )
    )
    assert pixiv_series == Path("dl/pixiv/魔法纪元_777/0001_序章_999.txt")

    pixiv_series_noindex = Path(
        build_path(
            1000,
            "番外",
            download_dir="dl",
            source="pixiv",
            series_id=777,
            series_name="魔法纪元",
            series_index=None,
        )
    )
    assert pixiv_series_noindex == Path("dl/pixiv/魔法纪元_777/番外_1000.txt")

    esj_series = Path(
        build_path(
            -42,
            "第一章",
            download_dir="dl",
            source="esj",
            series_id=-10,
            series_name="異世界",
            series_key="100",
            series_index=2,
        )
    )
    assert esj_series == Path("dl/esj/異世界_100/0002_第一章_-42.txt")

    reused_dir = Path("dl/esj/固定目录_100")
    assert Path(
        build_path(
            -43,
            "第二章",
            download_dir="dl",
            source="esj",
            series_id=-10,
            series_index=3,
            series_dir=reused_dir,
        )
    ) == Path("dl/esj/固定目录_100/0003_第二章_-43.txt")

    # Series directory detection ignores 4-digit Pixiv shards and flat source root; deletion cleans nested empty dirs
    from copixiv.core.naming import source_subdir
    from copixiv.storage.file_storage import FileStorage

    assert source_subdir("../bad") == ".._bad" or ".." not in source_subdir("bad")
    dl_root = tmp_path / "dl_layout"
    fs = FileStorage(str(dl_root))
    assert fs.find_existing_series_dir([str(dl_root / "pixiv" / "0001" / "Solo_1.txt")], "pixiv") is None
    assert fs.find_existing_series_dir([str(dl_root / "esj" / "Flat_1.txt")], "esj") is None
    found_dir = fs.find_existing_series_dir(
        [str(dl_root / "pixiv" / "旧名_777" / "0001_Cap_1.txt")],
        "pixiv",
    )
    assert found_dir == (dl_root / "pixiv" / "旧名_777").resolve()

    nested = dl_root / "esj" / "Book_100"
    nested.mkdir(parents=True)
    ch_txt = nested / "0001_Ch1_-1.txt"
    ch_txt.write_text("hello", encoding="utf-8")
    fs.delete_novel_files(str(ch_txt))
    assert not ch_txt.exists()
    assert not nested.exists()
    assert not (dl_root / "esj").exists()
    assert dl_root.exists()

    # Traversal attempts cannot escape download_dir
    base = tmp_path / "dl"
    for bad_source in ("../escape", "esj/sub", "..", "/abs/path", "a\\b"):
        built = Path(build_path(1, "T", download_dir=str(base), source=bad_source))
        assert ".." not in built.parts
        assert built.resolve().parent.parent == base.resolve()

    # {source} placeholder in NovelNamingTemplate
    tpl = NovelNamingTemplate("{source}/{author_name}/{title}_{id}")
    with_source = SimpleNamespace(id=10, title="Book", author_name="Auth", source="esj")
    assert tpl.resolve(with_source) == "esj/Auth/Book_10"

    without_source = SimpleNamespace(id=11, title="Book", author_name="Auth", source=None)
    assert tpl.resolve(without_source) == "Auth/Book_11"

    missing_attr = SimpleNamespace(id=12, title="Book", author_name="Auth")
    assert tpl.resolve(missing_attr) == "Auth/Book_12"


def test_negative_id_assets_file_storage_and_orphan_cleanup(tmp_path: Path):
    from copixiv.storage.orphan_cleanup import extract_managed_novel_id

    for valid in ("0", "123", "-456"):
        assert assets.is_novel_id_str(valid) is True
    for invalid in ("", "-", "abc", "+123", "12.3", "123\n", " 1"):
        assert assets.is_novel_id_str(invalid) is False

    # Negative-ID assets alongside positive-ID assets with same digits
    neg_cover = assets.asset_path(tmp_path, -456, assets.COVER, suffix=".png")
    neg_u = assets.asset_path(tmp_path, -456, assets.UPLOADED, "1", suffix=".jpg")
    neg_p = assets.asset_path(tmp_path, -456, assets.ILLUSTRATION, "2", suffix=".gif")
    pos_u = assets.asset_path(tmp_path, 456, assets.UPLOADED, "1", suffix=".jpg")
    neg_tmp = tmp_path / "-456_u_9.jpg.tmp"
    for p in (neg_cover, neg_u, neg_p, pos_u, neg_tmp):
        p.write_bytes(b"img")

    assert assets.novel_id_of(neg_cover.stem) == -456
    assert assets.novel_id_of(neg_u.stem) == -456
    assert assets.novel_id_of(neg_p.stem) == -456
    assert assets.find_cover(tmp_path, -456) == neg_cover
    assert assets.has_illustration_assets(tmp_path, -456) is True
    assert assets.scan_assets(tmp_path, -456) == {"1": neg_u, "2": neg_p}
    assert set(assets.iter_batch_novel_asset_files(tmp_path, [-456, "bad", "-"])) == {
        neg_cover,
        neg_u,
        neg_p,
    }
    assert set(assets.iter_novel_asset_files(tmp_path, -456)) == {neg_cover, neg_u, neg_p}

    # Cover-only negative-ID directory has_illustration_assets -> False
    cover_only_dir = tmp_path / "cover_only"
    cover_only_dir.mkdir()
    (cover_only_dir / "-777_c_cover.jpg").write_bytes(b"c")
    assert assets.has_illustration_assets(cover_only_dir, -777) is False

    # orphan_cleanup.extract_managed_novel_id handles negative text and asset stems
    assert extract_managed_novel_id("local_title_-123") == -123
    assert extract_managed_novel_id("-123_c_cover") == -123
    assert extract_managed_novel_id("-123_u_1") == -123
    assert extract_managed_novel_id("-123_p_99") == -123
    assert extract_managed_novel_id("bad_-") is None
    assert extract_managed_novel_id("bad_") is None
    assert extract_managed_novel_id("notes") is None
    assert extract_managed_novel_id("batch_export_-1") is None

    # FileStorage with source="esj" + deleting negative-ID cover/image assets
    dl_root = tmp_path / "dl"
    storage = FileStorage(str(dl_root))
    assert storage.novel_text_path(42, "ESJ", source="esj") == dl_root / "esj" / "ESJ_42.txt"
    saved_esj = storage.save_novel_text(42, "ESJ", "ESJ正文", source="esj")
    assert saved_esj == dl_root / "esj" / "ESJ_42.txt"
    assert storage.read_novel_text(42, "ESJ", source="esj") == "ESJ正文"

    neg_txt = storage.local_novel_path(-123, "LocalBook")
    neg_txt.parent.mkdir(parents=True, exist_ok=True)
    neg_txt.write_text("本地正文", encoding="utf-8")
    neg_epub = neg_txt.with_suffix(".epub")
    neg_epub.write_bytes(b"epub")
    neg_assets = [
        neg_txt.parent / "-123_c_cover.jpg",
        neg_txt.parent / "-123_u_1.png",
        neg_txt.parent / "-123_p_2.gif",
    ]
    for asset in neg_assets:
        asset.write_bytes(b"a")

    storage.delete_novel_files(str(neg_txt))
    assert not neg_txt.exists()
    assert not neg_epub.exists()
    for asset in neg_assets:
        assert not asset.exists()
    assert not neg_txt.parent.exists()

    # Batch delete cleans up negative-ID and positive-ID assets too
    neg_txt2 = storage.local_novel_path(-200, "LocalBatch")
    neg_txt2.parent.mkdir(parents=True, exist_ok=True)
    neg_txt2.write_text("b1", encoding="utf-8")
    neg_asset2 = neg_txt2.parent / "-200_u_5.jpg"
    neg_asset2.write_bytes(b"img")
    storage.delete_novels_files([str(neg_txt2), str(saved_esj)])
    assert not neg_txt2.exists()
    assert not neg_asset2.exists()
    assert not saved_esj.exists()


def test_epub_builder_negative_id_partial_and_allow_missing_images(tmp_path: Path):
    import zipfile

    from PIL import Image

    from copixiv.core.draft import build_novel
    from copixiv.storage.epub.builder import EpubBuilder

    novel_id = -123
    draft = build_novel(
        id=novel_id,
        title="负ID插图",
        author_id=-10,
        author_name="本地作者",
        content="段1\n[uploadedimage:1]\n段2\n[uploadedimage:2]",
        source="esj",
        download_dir=str(tmp_path),
    )
    txt_path = Path(draft.path)
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(draft.content or "", encoding="utf-8")

    cover_file = txt_path.parent / f"{novel_id}_c_cover.jpg"
    img_file = txt_path.parent / f"{novel_id}_u_1.png"
    Image.new("RGB", (12, 12), (10, 20, 30)).save(cover_file, format="JPEG")
    Image.new("RGBA", (12, 12), (200, 10, 10, 255)).save(img_file, format="PNG")

    # 1. Scanning parent_dir without manifest dicts resolves negative-ID cover + partial image
    res_scan = EpubBuilder().create_epub(draft)
    assert res_scan.ok is True
    assert res_scan.written is True
    assert res_scan.has_cover is True
    assert res_scan.embedded_images == 1
    assert res_scan.missing_images == 1
    with zipfile.ZipFile(txt_path.with_suffix(".epub")) as zf:
        assert "EPUB/cover.jpg" in zf.namelist()
        assert "EPUB/images/1.jpg" in zf.namelist()
        xhtml = zf.read("EPUB/content.xhtml").decode("utf-8")
        assert "illust-missing" in xhtml

    # 2. Passing asset_paths resolves negative-ID cover + image identically
    res_passed = EpubBuilder().create_epub(draft, asset_paths=[cover_file, img_file])
    assert res_passed.ok is True
    assert res_passed.written is True
    assert res_passed.has_cover is True
    assert res_passed.embedded_images == 1
    assert res_passed.missing_images == 1

    # 3. Cover-only when body image is missing: default keeps STRICT_PLACEHOLDERS guard,
    #    allow_missing_images=True emits cover + placeholder EPUB.
    img_file.unlink()
    sentinel = b"preserve-existing"
    epub_path = txt_path.with_suffix(".epub")
    epub_path.write_bytes(sentinel)

    res_strict = EpubBuilder().create_epub(draft)
    assert res_strict.ok is False
    assert res_strict.written is False
    assert res_strict.has_cover is True
    assert res_strict.embedded_images == 0
    assert res_strict.missing_images == 2
    assert epub_path.read_bytes() == sentinel

    res_best_effort = EpubBuilder().create_epub(draft, allow_missing_images=True)
    assert res_best_effort.ok is True
    assert res_best_effort.written is True
    assert res_best_effort.has_cover is True
    assert res_best_effort.embedded_images == 0
    assert res_best_effort.missing_images == 2
    assert res_best_effort.has_raw_markers is False
    with zipfile.ZipFile(epub_path) as zf:
        assert "EPUB/cover.jpg" in zf.namelist()
        assert "illust-missing" in zf.read("EPUB/content.xhtml").decode("utf-8")


def test_search_history_resolves_negative_author_and_series_ids():
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    from copixiv.features.search_history import record_search_history

    session = MagicMock()
    uow = MagicMock()

    @contextmanager
    def _begin():
        yield

    uow.begin = _begin
    uow.session = session

    with (
        patch("copixiv.features.search_history.SQLAlchemyAuthorRepository") as author_cls,
        patch("copixiv.features.search_history.SQLAlchemySeriesRepository") as series_cls,
        patch("copixiv.features.search_history.SQLAlchemySearchHistoryRepository") as history_cls,
    ):
        author_cls.return_value.get_by_id.return_value = {"author_name": "ESJAuthor"}
        series_cls.return_value.get_by_id.return_value = {"series_name": "ESJSeries"}

        record_search_history(
            [
                ("author_id", "-456"),
                ("series_id", "-789"),
                ("author_id", "abc"),
                ("author_id", ""),
                ("series_id", "-"),
            ],
            lambda: uow,
        )

    author_cls.return_value.get_by_id.assert_called_once_with(-456)
    series_cls.return_value.get_by_id.assert_called_once_with(-789)
    history_repo = history_cls.return_value
    history_repo.add_or_update.assert_any_call("author_id", "-456", "ESJAuthor")
    history_repo.add_or_update.assert_any_call("series_id", "-789", "ESJSeries")
    history_repo.add_or_update.assert_any_call("author_id", "abc", None)
    history_repo.add_or_update.assert_any_call("author_id", "", None)
    history_repo.add_or_update.assert_any_call("series_id", "-", None)
