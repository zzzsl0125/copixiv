"""M2/M3 复现：删除路径的 tag.reference_count 漂移 + delete_novel_fts 缺守卫。"""

import asyncio

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database.models import Base
from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository
from copixiv.infrastructure.repositories.author import SQLAlchemyAuthorRepository


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# M2 -------------------------------------------------------------


def _novel_payload():
    return {
        "id": 100, "title": "t", "author_id": 1,
        "path": "download/x/100.txt", "tag": ["R-18"],
        "shuffle": 5,
    }


def test_delete_novel_decrements_tag_reference_count(factory):
    """期望：删除小说后，其标签的 reference_count 递减到 0。"""
    async def scenario():
        with factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            await repo.upsert_novels([_novel_payload()])
            s.commit()
            tag = s.execute(select(models.Tag).where(models.Tag.name == "R-18")).scalars().one()
            assert tag.reference_count == 1

        with factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            await repo.delete(100)
            s.commit()
            tag = s.execute(select(models.Tag).where(models.Tag.name == "R-18")).scalars().one()
            assert tag.reference_count == 0, (
                f"删除后 reference_count 仍为 {tag.reference_count}（期望 0）"
            )

    asyncio.run(scenario())


def test_delete_author_decrements_tag_reference_count(factory):
    """期望：作者级删除同样递减计数。"""
    async def scenario():
        with factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            await repo.upsert_novels([_novel_payload()])
            s.commit()
            tag = s.execute(select(models.Tag).where(models.Tag.name == "R-18")).scalars().one()
            assert tag.reference_count == 1

        with factory() as s:
            repo = SQLAlchemyAuthorRepository(s)
            await repo.delete_author_and_data(1)
            s.commit()
            tag = s.execute(select(models.Tag).where(models.Tag.name == "R-18")).scalars().one()
            assert tag.reference_count == 0, (
                f"作者删除后 reference_count 仍为 {tag.reference_count}（期望 0）"
            )

    asyncio.run(scenario())


# M3 -------------------------------------------------------------


def test_delete_novel_works_when_fts_table_missing(factory):
    """期望：FTS 表不存在时删除小说仍然成功（跳过 FTS 清理）。"""
    async def scenario():
        with factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            # 用一个没有 novel_fts 表的干净库（create_all 不建 FTS 虚拟表）
            await repo.upsert_novels([_novel_payload()])
            s.commit()
        with factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            await repo.delete(100)  # 当前实现会抛 no such table: novel_fts
            s.commit()
            remaining = s.execute(select(models.Novel)).scalars().all()
            assert remaining == []

    asyncio.run(scenario())


def test_delete_author_works_when_fts_table_missing(factory):
    """期望：FTS 表不存在时作者级删除同样成功。"""
    async def scenario():
        with factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            await repo.upsert_novels([_novel_payload()])
            s.commit()
        with factory() as s:
            repo = SQLAlchemyAuthorRepository(s)
            await repo.delete_author_and_data(1)
            s.commit()
            remaining = s.execute(select(models.Novel)).scalars().all()
            assert remaining == []

    asyncio.run(scenario())
