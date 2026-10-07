"""迁移 0006 —— 存量本地行重编号（``id = -(时间坐标)``）的直测。

迁移跑在**真实库**上，而且改的是用户可见的 id 与磁盘文件名，属于一次性、不可逆
（只能靠 0006 的 downgrade 反算）的动作。测试库本身已经迁到 head，所以这里用
``importlib`` 按文件路径加载迁移模块，直接测它的纯函数与重编号过程 —— 覆盖
「插值 / 兜底 / 改名 / 两段式改号不撞主键 / 文件缺失时不动 path」五种情形。

权威：``docs/LIBRARY_DESIGN.md`` §3.1/§3.2 与 ``0006_local_id_time_axis`` 的
模块 docstring。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from copixiv.db.models import Novel

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "0006_local_id_time_axis.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0006", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mig = _load_migration()


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


def _seed_local(session_factory, novel_id: int, path: str) -> None:
    with session_factory() as session:
        session.add(Novel(
            id=novel_id, title=f"本地书{-novel_id}", author_name="未知作者",
            path=path, text=100, source="local",
            source_key=f"key{novel_id}", create_time=datetime.now(timezone.utc),
        ))
        session.commit()


def _local_rows(session_factory) -> list[tuple[int, str]]:
    with session_factory() as session:
        return [
            (int(r[0]), r[1])
            for r in session.execute(
                select(Novel.id, Novel.path).where(Novel.source == "local")
                .order_by(text("abs(id)"))
            ).all()
        ]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_revision_chain_is_linked_to_0005():
    assert mig.revision == "0006_local_id_time_axis"
    assert mig.down_revision == "0005_novel_source"


def test_seed_is_positive_and_grows_with_time():
    now = datetime.now(timezone.utc)
    assert mig._seed_coord(now) > 0
    assert mig._seed_coord(now + timedelta(days=30)) > mig._seed_coord(now)


def test_path_with_id_only_rewrites_the_conventional_suffix():
    assert mig._path_with_id("download/local/书_-999999999.txt", -999999999, -7) == \
        "download/local/书_-7.txt"
    # 不符约定（人工改过 / 没有号）→ 原样返回，不动
    assert mig._path_with_id("download/local/随手改的名.txt", -1, -2) == \
        "download/local/随手改的名.txt"
    assert mig._path_with_id(None, -1, -2) is None


def test_coord_for_time_interpolates_on_the_pixiv_axis(session_factory):
    stamps = [
        (1_000, datetime(2020, 1, 1, tzinfo=timezone.utc)),
        (2_000, datetime(2024, 1, 1, tzinfo=timezone.utc)),
    ]
    with session_factory() as session:
        for novel_id, when in stamps:
            session.add(Novel(
                id=novel_id, title=f"pixiv{novel_id}", path=f"download/{novel_id}.txt",
                text=100, source="pixiv", create_time=when,
            ))
        session.commit()
        conn = session.connection()

        assert mig._coord_for_time(conn, datetime(2023, 1, 1, tzinfo=timezone.utc)) == 1_000
        assert mig._coord_for_time(conn, datetime(2025, 1, 1, tzinfo=timezone.utc)) == 2_000
        # 早于全库最早的作品 / NULL → 0（调用方兜底种子）
        assert mig._coord_for_time(conn, datetime(2010, 1, 1, tzinfo=timezone.utc)) == 0
        assert mig._coord_for_time(conn, None) == 0


# ---------------------------------------------------------------------------
# 重编号：改号 + 改名 + 两段式
# ---------------------------------------------------------------------------


def test_renumber_rewrites_ids_paths_and_files(session_factory, tmp_path):
    """存量 ``-(10**9)+n`` → ``-(坐标)``，文件名跟着走（§3.2）。"""
    old_first, old_second = -999_999_999, -999_999_998
    first_file = tmp_path / f"旧书_{old_first}.txt"
    second_file = tmp_path / f"新书_{old_second}.txt"
    first_file.write_text("甲", encoding="utf-8")
    second_file.write_text("乙", encoding="utf-8")
    _seed_local(session_factory, old_first, str(first_file))
    _seed_local(session_factory, old_second, str(second_file))

    with session_factory() as session:
        rows = [(old_first, str(first_file), 100), (old_second, str(second_file), 200)]
        mapping = mig._renumber(
            session.connection(), rows, new_id_for=lambda coord: -coord
        )
        session.commit()

    assert mapping == [(old_first, -100), (old_second, -200)]
    assert _local_rows(session_factory) == [
        (-100, str(tmp_path / "旧书_-100.txt")),
        (-200, str(tmp_path / "新书_-200.txt")),
    ]
    assert not first_file.exists() and not second_file.exists()
    assert (tmp_path / "旧书_-100.txt").read_text(encoding="utf-8") == "甲"
    assert (tmp_path / "新书_-200.txt").read_text(encoding="utf-8") == "乙"


def test_renumber_survives_swapped_ids(session_factory, tmp_path):
    """两段式改号的理由：A 的新号正是 B 的旧号时，一趟 UPDATE 会撞主键。"""
    a_old, b_old = -10, -20
    _seed_local(session_factory, a_old, str(tmp_path / "a.txt"))
    _seed_local(session_factory, b_old, str(tmp_path / "b.txt"))

    with session_factory() as session:
        mig._renumber(
            session.connection(),
            [(a_old, str(tmp_path / "a.txt"), 20),   # -10 → -20（B 的旧号）
             (b_old, str(tmp_path / "b.txt"), 30)],  # -20 → -30
            new_id_for=lambda coord: -coord,
        )
        session.commit()

    assert _local_rows(session_factory) == [
        (-20, str(tmp_path / "a.txt")),
        (-30, str(tmp_path / "b.txt")),
    ]


def test_renumber_keeps_path_when_the_file_is_gone(session_factory, tmp_path):
    """文件找不到（用户删了 / CWD 不是项目根）→ **不动 path**。

    path 是读路径的真源：把它改成一个同样不存在的名字，才是真损坏。
    """
    missing = str(tmp_path / "不存在_ -999999999.txt".replace(" ", ""))
    _seed_local(session_factory, -999_999_999, missing)

    with session_factory() as session:
        mig._renumber(
            session.connection(), [(-999_999_999, missing, 5)],
            new_id_for=lambda coord: -coord,
        )
        session.commit()

    assert _local_rows(session_factory) == [(-5, missing)]


def test_renumber_refuses_to_clobber_an_existing_file(session_factory, tmp_path):
    """目标文件已存在 → 拒绝（宁可不迁，也不覆盖用户文件）。"""
    old_id = -999_999_999
    _seed_local(session_factory, old_id, str(tmp_path / f"书_{old_id}.txt"))
    (tmp_path / f"书_{old_id}.txt").write_text("原文", encoding="utf-8")
    (tmp_path / "书_-5.txt").write_text("别人的文件", encoding="utf-8")

    with session_factory() as session:
        with pytest.raises(RuntimeError, match="目标文件已存在"):
            mig._renumber(
                session.connection(), [(old_id, str(tmp_path / f"书_{old_id}.txt"), 5)],
                new_id_for=lambda coord: -coord,
            )
