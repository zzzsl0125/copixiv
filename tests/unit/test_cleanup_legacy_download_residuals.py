"""Unit tests for ``scripts/cleanup_legacy_download_residuals.py``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.cleanup_legacy_download_residuals import (
    NovelTargetRow,
    apply_cleanup_plan,
    build_cleanup_plan,
    format_report,
)


def _set_mtime(path: Path, ts: float) -> None:
    os.utime(path, (ts, ts))


def test_cleanup_plan_and_apply_handles_orphans_duplicates_overwrites_missing_and_unmanaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dl = Path("download")
    (dl / "pixiv" / "1234").mkdir(parents=True)
    (dl / "pixiv" / "2238").mkdir(parents=True)
    (dl / "pixiv" / "3333").mkdir(parents=True)
    (dl / "esj" / "book_1").mkdir(parents=True)
    (dl / "local").mkdir(parents=True)
    (dl / "download.zip").write_bytes(b"ZIP")

    shard_1234 = dl / "1234"
    shard_2238 = dl / "2238"
    shard_3333 = dl / "3333"
    shard_9999 = dl / "9999"
    for d in (shard_1234, shard_2238, shard_3333, shard_9999):
        d.mkdir(parents=True)

    # 1. Pure orphan not in DB
    orphan_txt = shard_1234 / "已删除_12340001.txt"
    orphan_txt.write_text("orphan", encoding="utf-8")
    orphan_epub = shard_1234 / "已删除_12340001.epub"
    orphan_epub.write_bytes(b"ORPHAN-EPUB")

    # 2. Duplicate where canonical target is newer or identical -> delete legacy duplicate
    canon_keep = dl / "pixiv" / "1234" / "新版保留_12340002.txt"
    canon_keep.write_text("canonical-newer", encoding="utf-8")
    _set_mtime(canon_keep, 2000.0)
    leg_older = shard_1234 / "旧标题_12340002.txt"
    leg_older.write_text("legacy-older", encoding="utf-8")
    _set_mtime(leg_older, 1000.0)

    # 3. Multiple legacy versions for the same novel ID where the newest beats canonical target -> overwrite_newer
    canon_stale = dl / "pixiv" / "2238" / "初版_22380003.txt"
    canon_stale.write_text("stale-v1", encoding="utf-8")
    _set_mtime(canon_stale, 1000.0)
    leg_v2 = shard_2238 / "修订二版_22380003.txt"
    leg_v2.write_text("updated-v2", encoding="utf-8")
    _set_mtime(leg_v2, 2000.0)
    leg_v3 = shard_2238 / "修订三版_22380003.txt"
    leg_v3.write_text("updated-v3-latest", encoding="utf-8")
    _set_mtime(leg_v3, 3000.0)

    # 4. Canonical target missing on disk -> move_missing for txt and has_epub==2 epub; delete epub when has_epub!=2
    missing_txt_target = dl / "pixiv" / "3333" / "缺失恢复_33330004.txt"
    leg_missing_txt = shard_3333 / "缺失恢复_33330004.txt"
    leg_missing_txt.write_text("restored-txt", encoding="utf-8")
    leg_missing_epub = shard_3333 / "缺失恢复_33330004.epub"
    leg_missing_epub.write_bytes(b"EPUB-NOT-EXPECTED")

    # 5. Unmanaged file inside shard_9999 -> skipped & keeps shard_9999 non-empty
    unmanaged = shard_9999 / "readme.txt"
    unmanaged.write_text("keep me", encoding="utf-8")
    orphan_9999 = shard_9999 / "orphan_99990005.txt"
    orphan_9999.write_text("bye", encoding="utf-8")

    # Unrelated non-4-digit dir at root must never be touched
    esj_file = dl / "esj" / "book_1" / "0001_ch_-1.txt"
    esj_file.write_text("esj", encoding="utf-8")

    targets = {
        12340002: NovelTargetRow(id=12340002, path="download/pixiv/1234/新版保留_12340002.txt", has_epub=3),
        22380003: NovelTargetRow(id=22380003, path="download/pixiv/2238/初版_22380003.txt", has_epub=3),
        33330004: NovelTargetRow(id=33330004, path="download/pixiv/3333/缺失恢复_33330004.txt", has_epub=3),
    }

    plan = build_cleanup_plan(targets, download_dir="download")
    report = format_report(plan, sample_limit=5)
    assert "试运行预览" in report
    assert plan.numeric_dirs_scanned == 4
    assert plan.action_counts["overwrite_newer"] == 1
    assert plan.action_counts["move_missing"] == 1
    assert plan.action_counts["delete_duplicate"] == 3  # leg_older, leg_v2, leg_missing_epub
    assert plan.action_counts["delete_orphan"] == 3  # orphan_txt, orphan_epub, orphan_9999
    assert plan.action_counts["skip_unmanaged"] == 1
    assert {p.name for p in plan.dirs_becoming_empty} == {"1234", "2238", "3333"}
    assert [p.name for p in plan.dirs_kept_nonempty] == ["9999"]

    # Dry-run leaves disk untouched
    assert orphan_txt.is_file()
    assert canon_stale.read_text(encoding="utf-8") == "stale-v1"

    applied = apply_cleanup_plan(plan)
    applied_report = format_report(plan, sample_limit=5, applied=applied)
    assert "执行模式" in applied_report
    assert applied.failed_items == []
    assert applied.overwritten_newer == 1
    assert applied.moved_missing == 1
    assert applied.deleted_duplicates == 3
    assert applied.deleted_orphans == 3
    assert applied.removed_empty_dirs == 3

    # Canonical files retain only the single latest version
    assert canon_keep.read_text(encoding="utf-8") == "canonical-newer"
    assert canon_stale.read_text(encoding="utf-8") == "updated-v3-latest"
    assert missing_txt_target.read_text(encoding="utf-8") == "restored-txt"
    assert not missing_txt_target.with_suffix(".epub").exists()

    # Emptied 4-digit dirs removed, non-empty 9999 preserved with unmanaged file
    assert not shard_1234.exists()
    assert not shard_2238.exists()
    assert not shard_3333.exists()
    assert shard_9999.is_dir()
    assert unmanaged.is_file()
    assert not orphan_9999.exists()

    # Non-legacy dirs and root files untouched
    assert esj_file.is_file()
    assert (dl / "download.zip").is_file()


def test_cleanup_skips_anomaly_when_db_path_still_in_legacy_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dl = Path("download")
    shard = dl / "1234"
    shard.mkdir(parents=True)
    legacy_file = shard / "未迁移_12340010.txt"
    legacy_file.write_text("keep-me", encoding="utf-8")

    targets = {
        12340010: NovelTargetRow(id=12340010, path="download/1234/未迁移_12340010.txt", has_epub=3),
    }

    plan = build_cleanup_plan(targets, download_dir="download")
    assert plan.action_counts["skip_anomaly"] == 1
    assert plan.dirs_becoming_empty == []
    applied = apply_cleanup_plan(plan)
    assert applied.deleted_duplicates == 0
    assert applied.removed_empty_dirs == 0
    assert legacy_file.is_file()
