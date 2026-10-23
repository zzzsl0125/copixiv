"""``db/base.py::update_summary`` — 作者 / 系列聚合的唯一入口。

这是在 ``author`` / ``series`` 上写 ``novel_count`` / ``like`` / ``view`` /
``text`` 的**唯一**实现（``features/novels/persist.py`` 的抓取收尾、编辑器挂上
作者 / 系列时的回写、以及删除路径都经由它）。它此前没有任何直接测试。

两条契约在这里钉住，各自都对应一个真实的缺陷形态：

* **删除必须反映到聚合上。** 聚合只从 ``novel`` 行推导，所以删掉一本之后没有
  任何东西会重算 ``author.novel_count`` / ``like`` / ``text`` —— 那些数字只会
  一直往上漂。删除路径（单本 / 批量）现在在同一个事务里重算受影响的行。
* **``ids=None`` 是空操作，不是"整张表"。** 调用方永远知道自己刚动了哪些行；
  一个能顺手"刷新全表"的辅助函数迟早会被一次无心的调用打中。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from copixiv.core.draft import NovelDraft
from copixiv.db import constants as C
from copixiv.db.base import update_summary
from copixiv.db.models import Author, Novel, Series
from copixiv.features.novels.persist import persist_novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.db.uow import SqlUnitOfWork


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


def _novel(novel_id: int, *, author_id=None, series_id=None, like=0, view=0, text=0):
    return NovelDraft(
        id=novel_id, title=f"n{novel_id}", author_id=author_id,
        series_id=series_id, like=like, view=view, text=text,
        path=f"download/0000/{novel_id}.txt",
    )


def _author(session_factory, author_id: int):
    with session_factory() as s:
        return s.get(Author, author_id)


def _series(session_factory, series_id: int):
    with session_factory() as s:
        return s.get(Series, series_id)


class TestAggregatesOnInsert:
    async def test_counters_and_derived_names_are_written(self, session_factory):
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await persist_novels(uow, [
                _novel(1, author_id=42, series_id=7, like=5, view=7, text=100),
                _novel(2, author_id=42, series_id=7, like=3, view=2, text=50),
            ])

        author = _author(session_factory, 42)
        assert (author.novel_count, author.like, author.view, author.text) == (
            2, 8, 9, 150,
        )
        series = _series(session_factory, 7)
        assert (series.novel_count, series.like, series.text) == (2, 8, 150)

    async def test_an_orphan_novel_contributes_to_no_row(self, session_factory):
        """``novel`` 行没有 author / series → 没有可聚合的目标，也不该报错。"""
        with session_factory() as s:
            # ``persist_novels`` 的入口要求 draft 带 author_id（占位行靠它建），
            # 所以孤儿行走仓库的普通插入，直接模拟"来源没给作者"的行。
            s.add(Novel(id=1, title="n1", path="download/0000/1.txt", like=5))
            s.commit()

        with session_factory() as s:
            update_summary(s, Author, C.COL_AUTHOR_ID, {None})
            update_summary(s, Series, C.COL_SERIES_ID, {None})
            s.commit()

        with session_factory() as s:
            assert s.execute(select(Author)).first() is None
            assert s.execute(select(Series)).first() is None


class TestAggregatesOnDelete:
    async def test_deleting_a_novel_lowers_the_author_and_series_counters(
        self, session_factory,
    ):
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await persist_novels(uow, [
                _novel(1, author_id=42, series_id=7, like=5, view=7, text=100),
                _novel(2, author_id=42, series_id=7, like=3, view=2, text=50),
            ])
        assert _author(session_factory, 42).novel_count == 2

        with session_factory() as s:
            await SQLAlchemyNovelRepository(s).delete(1)
            s.commit()

        # 剩下的那一本：id=2 / like=3 / view=2 / text=50
        author = _author(session_factory, 42)
        assert (author.novel_count, author.like, author.view, author.text) == (
            1, 3, 2, 50,
        )
        series = _series(session_factory, 7)
        assert (series.novel_count, series.like, series.text) == (1, 3, 50)

    async def test_deleting_the_last_novel_zeroes_instead_of_dangling(
        self, session_factory,
    ):
        """没有小说剩下时行仍要在（外键 / 用户可能再挂一本），计数归零。"""
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await persist_novels(uow, [_novel(1, author_id=42, series_id=7, like=5)])

        with session_factory() as s:
            await SQLAlchemyNovelRepository(s).delete(1)
            s.commit()

        author = _author(session_factory, 42)
        assert author is not None
        assert (author.novel_count, author.like, author.view, author.text) == (
            0, 0, 0, 0,
        )
        series = _series(session_factory, 7)
        assert series is not None and series.novel_count == 0

    async def test_batch_delete_refreshes_every_affected_parent(
        self, session_factory,
    ):
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await persist_novels(uow, [
                _novel(1, author_id=42, series_id=7, like=5),
                _novel(2, author_id=42, series_id=7, like=3),
                _novel(3, author_id=99, series_id=8, like=1),
            ])

        with session_factory() as s:
            await SQLAlchemyNovelRepository(s).delete_many([1, 2])
            s.commit()

        assert _author(session_factory, 42).novel_count == 0
        assert _series(session_factory, 7).novel_count == 0
        # 没被删的那一本与它的父行完全不受影响
        assert _author(session_factory, 99).novel_count == 1
        assert _series(session_factory, 8).novel_count == 1


class TestUpdateSummaryContract:
    def test_none_is_a_no_op_not_every_row(self, session_factory):
        """``ids=None`` 什么都不做 —— 它**不**是"刷新全表"。"""
        with session_factory() as s:
            s.add(Author(author_id=42, novel_count=9))
            s.add(Novel(id=1, title="t", author_id=42, path="download/0000/1.txt"))
            s.commit()

        with session_factory() as s:
            update_summary(s, Author, C.COL_AUTHOR_ID, None)
            s.commit()

        # 仍然是那个（故意写错的）值：没有任何行被重新计算。
        assert _author(session_factory, 42).novel_count == 9

    def test_empty_id_set_is_a_no_op(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=42, novel_count=9))
            s.commit()

        with session_factory() as s:
            update_summary(s, Author, C.COL_AUTHOR_ID, set())
            s.commit()

        assert _author(session_factory, 42).novel_count == 9
