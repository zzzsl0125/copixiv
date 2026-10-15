"""有界计数（``repo.COUNT_CAP`` / ``NovelCount``）的契约测试。

`GET /api/novels/count` 不再跑精确 ``COUNT(*)`` —— 它只回答「到没到上限」。
本文件钉住这条语义的三件事：

1. ``COUNT_CAP`` 与同步批量操作上限 ``BATCH_MAX_NOVELS`` 必须同值
   （两处各有一个字面量，靠这个测试防漂移）；
2. 命中数 **≤ 上限** → 精确值 + ``truncated=False``；**> 上限** → 值封顶为
   ``COUNT_CAP`` + ``truncated=True``；恰好等于上限仍算精确（内层多取的那
   一行就是为了这个边界）；
3. 被厌恶标签排除数（``count_excluded_novels``）走同一套有界语义。

上限用 monkeypatch 调到 2/3 这种小值，避免测试真的插 5001 行。
"""

from __future__ import annotations

import pytest

from copixiv.core.services import QuerySpec
from copixiv.db.models import Author, Novel, TagPreference
from copixiv.features.novels import batch_operations
from copixiv.features.novels import repo as repo_module
from copixiv.features.novels.repo import NovelCount, SQLAlchemyNovelRepository


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


def _repo(session) -> SQLAlchemyNovelRepository:
    return SQLAlchemyNovelRepository(session)


def _seed(session_factory, count: int, *, tags=(), like: int = 10) -> None:
    """插入 *count* 本小说（id 从 1 开始；tag 触发器自行维护 reference_count）。"""
    with session_factory() as s:
        for novel_id in range(1, count + 1):
            s.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
            s.flush()
            s.add(Novel(
                id=novel_id, title=f"标题{novel_id}", author_id=novel_id,
                author_name=f"作者{novel_id}", path=f"/tmp/{novel_id}.txt",
                has_epub=0, tags=list(tags), like=like,
            ))
        s.commit()


def _block(session_factory, tag: str) -> None:
    with session_factory() as s:
        s.add(TagPreference(tag=tag, preference="blocked"))
        s.commit()


def test_count_cap_matches_batch_cap():
    """计数上限 = 同步批量操作上限（同一个数值，跨模块不许漂移）。"""
    assert repo_module.COUNT_CAP == batch_operations.BATCH_MAX_NOVELS


async def test_under_cap_is_exact(session_factory):
    _seed(session_factory, 3)
    with session_factory() as s:
        result = await _repo(s).count_novels(QuerySpec())
    assert result == NovelCount(3, truncated=False)


async def test_exactly_at_cap_is_not_truncated(session_factory, monkeypatch):
    """边界：命中数恰好 == 上限 → 精确、不标 truncated。"""
    _seed(session_factory, 3)
    monkeypatch.setattr(repo_module, "COUNT_CAP", 3)
    with session_factory() as s:
        result = await _repo(s).count_novels(QuerySpec())
    assert result == NovelCount(3, truncated=False)


async def test_over_cap_is_flagged(session_factory, monkeypatch):
    """> 上限：值封顶为 COUNT_CAP，truncated=True（值是下界，不是精确值）。"""
    _seed(session_factory, 3)
    monkeypatch.setattr(repo_module, "COUNT_CAP", 2)
    with session_factory() as s:
        result = await _repo(s).count_novels(QuerySpec())
    assert result == NovelCount(2, truncated=True)


async def test_filters_share_the_bound(session_factory, monkeypatch):
    """过滤条件（tag / exclude_ids）走同一条有界路径。"""
    _seed(session_factory, 3, tags=["NTR"])
    monkeypatch.setattr(repo_module, "COUNT_CAP", 2)
    with session_factory() as s:
        repo = _repo(s)
        # tags 命中 3 本 > 上限 2 → 截断
        tagged = await repo.count_novels(
            QuerySpec(conditions=[("tags", "NTR")])
        )
        # exclude_ids 把命中集降到 1 本 ≤ 上限 → 精确
        excluded = await repo.count_novels(QuerySpec(exclude_ids=[1, 2]))
    assert tagged == NovelCount(2, truncated=True)
    assert excluded == NovelCount(1, truncated=False)


async def test_excluded_count_is_bounded_too(session_factory, monkeypatch):
    """``count_excluded_novels``（with_excluded 路径）同样有界。"""
    _seed(session_factory, 3, tags=["NTR"])
    _block(session_factory, "NTR")
    with session_factory() as s:
        exact = await _repo(s).count_excluded_novels(QuerySpec())
    assert exact == NovelCount(3, truncated=False)

    monkeypatch.setattr(repo_module, "COUNT_CAP", 2)
    with session_factory() as s:
        bounded = await _repo(s).count_excluded_novels(QuerySpec())
    assert bounded == NovelCount(2, truncated=True)


async def test_excluded_count_zero_when_exclusion_off(session_factory):
    """排除关闭时是精确 0（不查库、不标 truncated）。"""
    _seed(session_factory, 3, tags=["NTR"])
    with session_factory() as s:
        off = await _repo(s).count_excluded_novels(
            QuerySpec(exclude_blocked_tags=False)
        )
    assert off == NovelCount(0, truncated=False)
