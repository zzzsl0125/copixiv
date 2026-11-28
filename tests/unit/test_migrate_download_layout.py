"""Unit tests for ``scripts/migrate_download_layout.py``."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.migrate_download_layout import (
    NovelMigrationRow,
    apply_migration_plan,
    build_migration_plan,
    format_report,
)
from sqlalchemy import create_engine


def _make_row(
    *,
    nid: int,
    title: str,
    path: str | None,
    source: str = "pixiv",
    series_id: int | None = None,
    novel_series_name: str | None = None,
    series_index: int | None = None,
    has_epub: int = 0,
    series_name: str | None = None,
    series_source_key: str | None = None,
) -> NovelMigrationRow:
    return NovelMigrationRow(
        id=nid,
        title=title,
        source=source,
        series_id=series_id,
        novel_series_name=novel_series_name,
        series_index=series_index,
        path=path,
        has_epub=has_epub,
        series_name=series_name,
        series_source_key=series_source_key,
    )


def _sqlite_db_for_rows(rows: list[NovelMigrationRow]):
    engine = create_engine("sqlite:///:memory:")
    # Create only the tables needed for update(Novel) without PG-only DDL issues,
    # using manual SQLite table schema matching Novel columns used in migration.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE novel (
                id BIGINT PRIMARY KEY,
                title TEXT NOT NULL,
                author_id BIGINT,
                author_name TEXT,
                series_id BIGINT,
                series_name TEXT,
                series_index INTEGER,
                tags TEXT,
                text INTEGER DEFAULT 0,
                "like" INTEGER DEFAULT 0,
                "view" INTEGER DEFAULT 0,
                has_epub INTEGER DEFAULT 0,
                is_favourite BOOLEAN DEFAULT 0,
                create_time TEXT,
                path TEXT,
                source TEXT DEFAULT 'pixiv',
                source_key TEXT
            )
            """
        )
        for r in rows:
            conn.exec_driver_sql(
                "INSERT INTO novel (id, title, series_id, series_name, series_index, path, has_epub, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.id,
                    r.title,
                    r.series_id,
                    r.novel_series_name,
                    r.series_index,
                    r.path,
                    r.has_epub,
                    r.source,
                ),
            )
    return engine


def test_dry_run_plan_and_apply_across_all_legacy_buckets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    dl = Path("download")

    # 1. Old Pixiv standalone under shard 1234 with .txt, .epub, and cover/u/p assets
    shard_dir = dl / "1234"
    shard_dir.mkdir(parents=True)
    p1_old = shard_dir / "孤岛物语_12345678.txt"
    p1_old.write_text("story 1", encoding="utf-8")
    p1_epub = shard_dir / "孤岛物语_12345678.epub"
    p1_epub.write_bytes(b"EPUB1")
    (shard_dir / "12345678_c_cover.jpg").write_bytes(b"JPG")
    (shard_dir / "12345678_u_11.png").write_bytes(b"PNG")
    (shard_dir / "12345678_p_22.webp").write_bytes(b"WEBP")

    # 2. Root-level legacy Pixiv file directly under download/
    p2_old = dl / "根目录老作品_20842532.txt"
    p2_old.write_text("root legacy", encoding="utf-8")

    # 3. Pixiv series with empty series.series_name (both chapters share one canonical folder!)
    s1_old = shard_dir / "序章_12340001.txt"
    s1_old.write_text("ch1", encoding="utf-8")
    s2_old = dl / "1235" / "第二章_12350002.txt"
    s2_old.parent.mkdir(parents=True)
    s2_old.write_text("ch2", encoding="utf-8")
    (s2_old.parent / "12350002_u_99.jpg").write_bytes(b"IMG")

    # 4. ESJ series currently flat under download/esj/
    esj_dir = dl / "esj"
    esj_dir.mkdir(parents=True)
    esj_old = esj_dir / "1.起风了_-29738852.txt"
    esj_old.write_text("esj ch1", encoding="utf-8")

    # 5. Already canonical local standalone (remains untouched)
    local_dir = dl / "local"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "本地自定义名_-5.txt"
    local_file.write_text("local body", encoding="utf-8")

    # 6. Missing file on disk (never touches DB)
    missing_path = "download/9999/消失的书_99990001.txt"

    # 7. Unmanaged files at download root
    (dl / "download.zip").write_bytes(b"ZIP")
    (dl / "broken_epub_ids.txt").write_text("123\n", encoding="utf-8")

    rows = [
        _make_row(nid=12345678, title="孤岛物语", path=str(p1_old), has_epub=2),
        _make_row(nid=20842532, title="根目录老作品", path=str(p2_old)),
        _make_row(
            nid=12340001,
            title="序章",
            path=str(s1_old),
            series_id=777,
            series_index=1,
            series_name="",
            series_source_key="777",
        ),
        _make_row(
            nid=12350002,
            title="第二章",
            path=str(s2_old),
            series_id=777,
            series_index=2,
            series_name="",
            series_source_key="777",
        ),
        _make_row(
            nid=-29738852,
            title="1.起风了",
            source="esj",
            path=str(esj_old),
            series_id=-1,
            novel_series_name="风之物语",
            series_index=1,
            series_name="风之物语",
            series_source_key="1602082116",
        ),
        _make_row(nid=-5, title="改过名但已在local目录", source="local", path=str(local_file)),
        _make_row(nid=99990001, title="消失的书", path=missing_path),
    ]

    plan = build_migration_plan(rows, download_dir="download")
    report = format_report(plan, sample_limit=5)
    assert "试运行预览" in report
    assert "download.zip" in report
    assert plan.action_counts["to_move"] == 5
    assert plan.action_counts["already_canonical"] == 1
    assert plan.action_counts["missing_on_disk"] == 1
    assert plan.txt_move_count == 5
    assert plan.epub_move_count == 1
    assert plan.asset_move_count == 4
    # Dry-run left disk completely untouched
    assert p1_old.is_file()
    assert esj_old.is_file()

    # Apply with SQLite engine
    engine = _sqlite_db_for_rows(rows)
    try:
        res = apply_migration_plan(plan, engine, batch_size=2)
        assert res.failed_rows == []
        assert res.updated_db_rows == 5
        assert res.moved_txt == 5
        assert res.moved_epub == 1
        assert res.moved_assets == 4

        with engine.connect() as conn:
            db_paths = {int(r[0]): r[1] for r in conn.exec_driver_sql("SELECT id, path FROM novel")}

        assert db_paths[12345678] == "download/pixiv/1234/孤岛物语_12345678.txt"
        assert (tmp_path / db_paths[12345678]).is_file()
        assert (tmp_path / "download/pixiv/1234/孤岛物语_12345678.epub").is_file()
        assert (tmp_path / "download/pixiv/1234/12345678_c_cover.jpg").is_file()
        assert (tmp_path / "download/pixiv/1234/12345678_u_11.png").is_file()
        assert (tmp_path / "download/pixiv/1234/12345678_p_22.webp").is_file()

        assert db_paths[20842532] == "download/pixiv/2084/根目录老作品_20842532.txt"
        assert (tmp_path / db_paths[20842532]).is_file()

        # Empty-series-name Pixiv chapters both landed in ONE shared series directory
        assert db_paths[12340001] == "download/pixiv/序章_777/0001_序章_12340001.txt"
        assert db_paths[12350002] == "download/pixiv/序章_777/0002_第二章_12350002.txt"
        assert (tmp_path / "download/pixiv/序章_777/12350002_u_99.jpg").is_file()

        # ESJ series uses series.source_key instead of negative series_id
        assert db_paths[-29738852] == "download/esj/风之物语_1602082116/0001_1.起风了_-29738852.txt"
        assert (tmp_path / db_paths[-29738852]).is_file()

        # Canonical local and missing row remain untouched
        assert db_paths[-5] == "download/local/本地自定义名_-5.txt"
        assert db_paths[99990001] == missing_path
        # Unmanaged files at download/ root are untouched
        assert (dl / "download.zip").is_file()
        assert (dl / "broken_epub_ids.txt").is_file()
        # Emptied old shard directories were removed; esj parent now holds the series dir
        assert not shard_dir.exists()
        assert not (dl / "1235").exists()
        assert esj_dir.is_dir()
        assert not esj_old.exists()
        assert shard_dir.resolve() not in plan.dirs_with_leftovers
        assert shard_dir.resolve() in plan.dirs_becoming_empty
        assert esj_dir.resolve() not in plan.dirs_becoming_empty

        # Re-running plan is now cleanly idempotent
        rows_after = [
            _make_row(
                nid=r.id,
                title=r.title,
                source=r.source,
                series_id=r.series_id,
                novel_series_name=r.novel_series_name,
                series_index=r.series_index,
                path=db_paths[r.id],
                has_epub=r.has_epub,
                series_name=r.series_name,
                series_source_key=r.series_source_key,
            )
            for r in rows
        ]
        plan2 = build_migration_plan(rows_after, download_dir="download")
        assert plan2.action_counts.get("to_move", 0) == 0
        assert plan2.action_counts["already_canonical"] == 6
        assert plan2.action_counts["missing_on_disk"] == 1
    finally:
        engine.dispose()


def test_migration_handles_conflicts_outside_root_and_db_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dl = Path("download")
    (dl / "1234").mkdir(parents=True)
    (dl / "pixiv" / "1234").mkdir(parents=True)

    # Conflict: both old and new txt exist
    src_conflict = dl / "1234" / "冲突书_12340010.txt"
    dst_conflict = dl / "pixiv" / "1234" / "冲突书_12340010.txt"
    src_conflict.write_text("OLD", encoding="utf-8")
    dst_conflict.write_text("NEW", encoding="utf-8")

    # DB resume: txt already at target, epub still at legacy location
    resumed_dst = dl / "pixiv" / "1234" / "续迁书_12340020.txt"
    resumed_dst.write_text("ALREADY MOVED", encoding="utf-8")
    resumed_old_epub = dl / "1234" / "续迁书_12340020.epub"
    resumed_old_epub.write_bytes(b"EPUB")

    rows = [
        _make_row(nid=12340010, title="冲突书", path=str(src_conflict)),
        _make_row(nid=12340020, title="续迁书", path="download/1234/续迁书_12340020.txt"),
        _make_row(nid=12340030, title="越界书", path="/etc/passwd"),
        _make_row(nid=12340040, title="空路径", path=None),
    ]

    plan = build_migration_plan(rows, download_dir="download")
    assert plan.action_counts["conflict"] == 1
    assert plan.action_counts["db_only_resume"] == 1
    assert plan.action_counts["outside_root"] == 2

    engine = _sqlite_db_for_rows(rows)
    try:
        res = apply_migration_plan(plan, engine)
        assert res.updated_db_rows == 1
        assert res.moved_txt == 0
        assert res.moved_epub == 1
        # Conflict files were not overwritten
        assert src_conflict.read_text(encoding="utf-8") == "OLD"
        assert dst_conflict.read_text(encoding="utf-8") == "NEW"
        # Resumed epub moved alongside resumed txt and DB path updated
        assert (dl / "pixiv" / "1234" / "续迁书_12340020.epub").is_file()
        assert not resumed_old_epub.exists()
        with engine.connect() as conn:
            path_resumed = conn.exec_driver_sql("SELECT path FROM novel WHERE id = 12340020").scalar_one()
        assert path_resumed == "download/pixiv/1234/续迁书_12340020.txt"
    finally:
        engine.dispose()
