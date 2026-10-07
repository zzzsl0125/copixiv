"""``SQLAlchemyNovelWriteRepository`` 两个批量元数据方法的**语句级**测试。

为什么是语句级：``update_metadata_many`` 的契约不只是"写对了值"，还有

* **一条** set-based UPDATE 覆盖整批（``sync_tag_refs_batch`` 是语句级触发器，
  批量编辑不许退化成 N 次单行写）；
* **不**逐行 ``get_by_id`` + ``setattr``、**不**走 ``_upsert_rows``（来源刷新的
  写路径，带来源权威白名单与"非空者胜"规则）。

值级断言看不出这两件事，所以这里用 ``before_cursor_execute`` 事件把真正发出去的
SQL 记下来数一数，并用 session 的 identity map 证明没有发生逐行加载。
``filter_ids_by_source`` 断言的是"收窄 + 升序 + 空输入短路"这三条形状。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Mapping

import pytest
from sqlalchemy import event

from copixiv.db.models import Author, Novel, Series, Tag
from copixiv.features.library.local_axis import SOURCE_LOCAL, SOURCE_PIXIV
from copixiv.features.novels.repo import (
    BATCH_METADATA_COLUMNS,
    SQLAlchemyNovelRepository,
)

LOCAL_IDS = [-999_000_001, -999_000_002]
PIXIV_IDS = [987_654, 987_655]
AUTHOR_ID = 555


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


@pytest.fixture(autouse=True)
def _quiet_count_recompute():
    """清掉进程级 count 缓存，让引擎级语句捕获不受后台重算线程干扰。

    ``repo._recompute_count_cache_after_write`` 会在**任何**写提交后起一个后台
    线程重算缓存里的 count（它发的是 ``SELECT count(*) FROM novel``）。语句级
    断言不该被这种无关噪声影响，所以这里把缓存清空——没有缓存键，那个线程根本
    不会起。
    """
    from copixiv.features.novels import repo as repo_module

    repo_module._count_cache.clear()
    yield
    repo_module._count_cache.clear()


@contextmanager
def _captured(engine):
    """记录这一段里真正发出去的 SQL（engine 级事件；用完即摘）。

    注意后台的 ``copixiv-count-recompute`` 线程会往同一个 engine 上发 SELECT，
    所以断言一律先按"这条语句是不是我关心的那个形状"筛一遍，而不是要求捕获窗口
    里**总共**只有一条语句。
    """
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _seed_novel(
    session_factory,
    novel_id: int,
    *,
    source: str = SOURCE_LOCAL,
    author_id: int | None = None,
    author_name: str | None = None,
    series_id: int | None = None,
    series_name: str | None = None,
    series_index: int | None = None,
    tags: list[str] | None = None,
    title: str | None = None,
    like: int = 0,
) -> None:
    with session_factory() as s:
        s.add(Novel(
            id=novel_id,
            title=title or f"书{novel_id}",
            author_id=author_id,
            author_name=author_name,
            series_id=series_id,
            series_name=series_name,
            series_index=series_index,
            path=f"download/repo/{novel_id}.txt",
            like=like, view=0, text=100,
            has_epub=3,
            tags=tags or [],
            source=source,
            source_key=(f"{abs(novel_id):064d}" if source == SOURCE_LOCAL else None),
        ))
        s.commit()


def _seed_author(session_factory, author_id: int, name: str | None = None) -> None:
    with session_factory() as s:
        s.add(Author(author_id=author_id, author_name=name))
        s.commit()


def _seed_series(session_factory, series_id: int, name: str | None = None) -> None:
    with session_factory() as s:
        s.add(Series(series_id=series_id, series_name=name))
        s.commit()


def _row(session_factory, novel_id: int) -> Novel:
    with session_factory() as s:
        return s.get(Novel, novel_id)


# ---------------------------------------------------------------------------
# filter_ids_by_source —— 收窄 / 顺序 / 空输入短路
# ---------------------------------------------------------------------------

async def test_filter_ids_by_source_narrows_to_the_requested_sources(
    session_factory,
):
    for novel_id in LOCAL_IDS:
        _seed_novel(session_factory, novel_id)
    for novel_id in PIXIV_IDS:
        _seed_novel(session_factory, novel_id, source=SOURCE_PIXIV)

    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        # 混着传、顺序打乱 → 只留下 local，且按 id 升序（升序是契约：
        # applicable 的来源顺序要可复现，见 build_ids 的同一个理由）
        assert await repo.filter_ids_by_source(
            [LOCAL_IDS[1], PIXIV_IDS[0], LOCAL_IDS[0]], {SOURCE_LOCAL},
        ) == sorted(LOCAL_IDS)
        assert await repo.filter_ids_by_source(
            [LOCAL_IDS[0], *PIXIV_IDS], {SOURCE_PIXIV},
        ) == sorted(PIXIV_IDS)
        # 两个来源一起要 → 全都要（调用方只传 {local}，这条是方法的完整语义）
        assert await repo.filter_ids_by_source(
            [*LOCAL_IDS, *PIXIV_IDS], {SOURCE_LOCAL, SOURCE_PIXIV},
        ) == sorted([*LOCAL_IDS, *PIXIV_IDS])
        # 空输入与"一个都不属于"都是空集，都不报错
        assert await repo.filter_ids_by_source([], {SOURCE_LOCAL}) == []
        assert await repo.filter_ids_by_source(LOCAL_IDS, set()) == []
        assert await repo.filter_ids_by_source([987_654_321], {SOURCE_LOCAL}) == []


async def test_filter_ids_by_source_is_a_single_indexed_select(
    session_factory, pg_engine,
):
    """一条 SELECT，条件里同时有 id 与 source（``ix_novel_source`` 走得到）。

    "一次查完"是刻意的：它给 ``applicable`` 报数，方法一旦分块/循环，预演的
    代价就跟着选择集大小线性涨。
    """
    for novel_id in LOCAL_IDS:
        _seed_novel(session_factory, novel_id)
    _seed_novel(session_factory, PIXIV_IDS[0], source=SOURCE_PIXIV)

    with session_factory() as s:
        with _captured(pg_engine) as seen:
            found = await SQLAlchemyNovelRepository(s).filter_ids_by_source(
                [*LOCAL_IDS, PIXIV_IDS[0]], {SOURCE_LOCAL},
            )

    assert found == sorted(LOCAL_IDS)
    selects = [q for q in seen if q.lstrip().upper().startswith("SELECT")]
    reads = [q for q in selects if "FROM novel" in q and "source IN" in q]
    assert len(reads) == 1, seen
    assert "ORDER BY novel.id" in reads[0], reads[0]


async def test_batch_id_lists_bind_as_one_array_not_inlined_placeholders(
    session_factory, pg_engine,
):
    """id 列表走 ``= ANY(:array)``，**不**展开成几千个 ``IN`` 占位符。

    为什么单钉一条：批量编辑的端点是全仓唯一**没有** 5000 上限的写路径
    （``resolve_batch_scope(cap=None)``——"全选匹配"在生产可以是 20 万级），而
    ``IN (...)`` 会把每个 id 渲染成一个绑定参数，语句文本与参数表都随选择集线性
    膨胀（数 MB 的 SQL、几万个绑定参数）。``= ANY(:array)`` 只传**一个**数组参数
    ——这也是 ``repo.py`` 模块 docstring 里写明的 PG 惯例（"``id = ANY($1)`` 取代
    SQLite 时代的 ``IN``"）。

    断言的是"语句怎么拼"：给一个 5000 元素的 id 列表（那些行不需要存在——真值
    断言由相邻测试覆盖），要求 SELECT 与 UPDATE 都恰好只有一个参数、且它的值是
    整个列表。
    """
    huge = list(range(-9_000_000, -8_995_000))
    captured: list[tuple[str, Mapping[str, Any]]] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if "FROM novel" in statement or "UPDATE novel" in statement:
            captured.append((statement, parameters))

    event.listen(pg_engine, "before_cursor_execute", _before)
    try:
        with session_factory() as s:
            repo = SQLAlchemyNovelRepository(s)
            await repo.filter_ids_by_source(huge, {SOURCE_LOCAL})
            await repo.update_metadata_many(huge, {"author_id": AUTHOR_ID})
    finally:
        event.remove(pg_engine, "before_cursor_execute", _before)

    assert len(captured) == 2, [q[:120] for q, _ in captured]
    for statement, parameters in captured:
        assert "ANY" in statement, statement[:300]
        # 一个占位符承载整个数组，而不是每个 id 一个
        assert statement.count("%(param_1)s") == 1, statement[:300]
        arrays = [v for v in parameters.values() if isinstance(v, (list, tuple))]
        assert len(arrays) == 1 and len(arrays[0]) == len(huge), parameters


# ---------------------------------------------------------------------------
# update_metadata_many —— 一条 set-based UPDATE / 计数只看真变化 / 键白名单
# ---------------------------------------------------------------------------

async def test_update_metadata_many_is_one_set_based_update(
    session_factory, pg_engine,
):
    """一条 UPDATE 覆盖三个 id：没有 INSERT（不是 ``_upsert_rows``）、没有逐行读。

    ``identity_map`` 为空是"没有逐行 ``get_by_id`` + ``setattr``"的直接证据
    ——那条件形态（一条语句 + 一次 ``RETURNING``）也正是语句级触发器只跑一次的
    前提。
    """
    for novel_id in LOCAL_IDS + [LOCAL_IDS[0] + 1]:
        _seed_novel(session_factory, novel_id, author_name="本地作者")
    _seed_author(session_factory, AUTHOR_ID, "库里已知的作者")
    ids = LOCAL_IDS + [LOCAL_IDS[0] + 1]

    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        with _captured(pg_engine) as seen:
            affected = await repo.update_metadata_many(
                ids, {"author_id": AUTHOR_ID, "author_name": "库里已知的作者"},
            )
        assert len(s.identity_map) == 0, "批量写不该把行读进 session"
        s.commit()

    assert affected == 3
    updates = [q for q in seen if q.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1, seen
    assert "RETURNING novel.id" in updates[0], updates[0]
    writes = [q for q in seen if "INSERT" in q.upper() or "ON CONFLICT" in q.upper()]
    assert writes == [], f"批量元数据写不许走 _upsert_rows: {writes}"

    for novel_id in ids:
        row = _row(session_factory, novel_id)
        assert (row.author_id, row.author_name) == (AUTHOR_ID, "库里已知的作者")
        # 只写 values 里那几列：结构性列与机器事实一个字节没动
        assert row.title == f"书{novel_id}"
        assert row.source == SOURCE_LOCAL
        assert row.like == 0
        assert row.tags == []


async def test_update_metadata_many_does_not_churn_tag_counts(
    session_factory, pg_engine,
):
    """非 tags 的 UPDATE 触发语句级触发器后立刻早退：``tag`` 行不受牵连。

    这是"一条语句"的实际收益（``add_tags_to_novels`` 的注释里那条理由）：
    几百本的作者编辑不会去锁任何 ``tag`` 行。
    """
    _seed_novel(session_factory, LOCAL_IDS[0], tags=["R-18"])
    _seed_author(session_factory, 777, "某作者")
    with session_factory() as s:
        assert s.query(Tag).filter_by(name="R-18").one().reference_count == 1

    with session_factory() as s:
        await SQLAlchemyNovelRepository(s).update_metadata_many(
            [LOCAL_IDS[0]], {"author_id": 777, "author_name": "某作者"},
        )
        s.commit()

    with session_factory() as s:
        assert s.query(Tag).filter_by(name="R-18").one().reference_count == 1
    assert _row(session_factory, LOCAL_IDS[0]).tags == ["R-18"]


async def test_update_metadata_many_counts_only_rows_that_really_changed(
    session_factory,
):
    """``RETURNING`` + 逐列 ``IS DISTINCT FROM``：没变化的行不算"改过"。

    没有这层收窄，重复提交同一请求第二次还会报"改了 3 本"——用户没法用
    ``affected`` 判断这次到底有没有动库。
    """
    for novel_id in LOCAL_IDS:
        _seed_novel(session_factory, novel_id, author_name="本地作者")
    third_id = LOCAL_IDS[0] + 1
    _seed_novel(session_factory, third_id, author_name="本地作者")
    _seed_author(session_factory, AUTHOR_ID, "某作者")

    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        first = await repo.update_metadata_many(
            LOCAL_IDS, {"author_id": AUTHOR_ID, "author_name": "某作者"},
        )
        second = await repo.update_metadata_many(
            LOCAL_IDS, {"author_id": AUTHOR_ID, "author_name": "某作者"},
        )
        # 两本已经挂上了，第三本还没有 → 只有 1 行真的变了
        third = await repo.update_metadata_many(
            [LOCAL_IDS[0], third_id], {"author_id": AUTHOR_ID},
        )
        s.commit()

    assert (first, second, third) == (2, 0, 1)


async def test_update_metadata_many_null_only_guard_fills_missing_values(
    session_factory,
):
    """``only_where_null``：``author_name IS NULL`` 的行补名字，有名字的不动。

    批量解除作者关联的第二步就是它——"保留显示名、只补待解析的行"在 SQL 里
    就是这一条带条件的 UPDATE（单本语义里是"读这一行的现值再分支"）。
    """
    _seed_novel(session_factory, LOCAL_IDS[0], author_name="文件名解析出的作者")
    _seed_novel(session_factory, LOCAL_IDS[1], author_name=None)
    ids = LOCAL_IDS

    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        changed = await repo.update_metadata_many(
            ids, {"author_name": "未知作者"}, only_where_null="author_name",
        )
        s.commit()

    assert changed == 1
    assert _row(session_factory, LOCAL_IDS[0]).author_name == "文件名解析出的作者"
    assert _row(session_factory, LOCAL_IDS[1]).author_name == "未知作者"


async def test_update_metadata_many_clears_the_series_triple(
    session_factory,
):
    """解除关联的列集合由调用方给（本层只负责"一条 UPDATE 写这几列"）。"""
    _seed_series(session_factory, 606, "某个系列")
    _seed_novel(
        session_factory, LOCAL_IDS[0],
        series_id=606, series_name="某个系列", series_index=3,
    )

    with session_factory() as s:
        changed = await SQLAlchemyNovelRepository(s).update_metadata_many(
            [LOCAL_IDS[0]],
            {"series_id": None, "series_name": None, "series_index": None},
        )
        s.commit()

    assert changed == 1
    row = _row(session_factory, LOCAL_IDS[0])
    assert (row.series_id, row.series_name, row.series_index) == (None, None, None)


@pytest.mark.parametrize("values", [
    {"title": "想改标题"},
    {"caption": "想改简介"},
    {"tags": []},
    {"like": 999},
    {"path": "download/x.txt"},
    {"id": 1},
    {"author_ids": [1]},          # 拼错的键名同样是"不在白名单"
])
async def test_update_metadata_many_rejects_columns_outside_the_whitelist(
    session_factory, values,
):
    """白名单是唯一所有者（``BATCH_METADATA_COLUMNS``）：越界键 ``ValueError``。

    API 层先行 422（``extra="forbid"``），这一层拦的是"绕过 HTTP 直接调用例 /
    仓库"的路径——写错列的静默后果比报错严重得多。
    """
    _seed_novel(session_factory, LOCAL_IDS[0], title="原标题")

    with session_factory() as s:
        with pytest.raises(ValueError, match="不支持的批量元数据字段"):
            await SQLAlchemyNovelRepository(s).update_metadata_many(
                [LOCAL_IDS[0]], values,
            )

    row = _row(session_factory, LOCAL_IDS[0])
    assert (row.title, row.like, row.path) == (
        "原标题", 0, f"download/repo/{LOCAL_IDS[0]}.txt",
    )


def test_whitelist_is_exactly_the_author_and_series_columns():
    """白名单只有作者 / 系列那五列 —— 它是一份**清单**，不是一句描述。"""
    assert BATCH_METADATA_COLUMNS == {
        "author_id", "author_name", "series_id", "series_name", "series_index",
    }


async def test_only_where_null_must_be_a_whitelisted_column_in_values(
    session_factory,
):
    """条件列必须是本次 values 里的白名单列——否则这条 UPDATE 没有主语。"""
    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        with pytest.raises(ValueError, match="only_where_null"):
            await repo.update_metadata_many(
                [LOCAL_IDS[0]],
                {"author_name": "未知作者"},
                only_where_null="title",
            )
        with pytest.raises(ValueError, match="only_where_null"):
            await repo.update_metadata_many(
                [LOCAL_IDS[0]], {"author_id": 1}, only_where_null="author_name",
            )


async def test_empty_inputs_issue_no_statement(session_factory, pg_engine):
    """空 id 列表 / 空 values 都是"没有要写的东西"：不发语句、返回 0。"""
    _seed_novel(session_factory, LOCAL_IDS[0], author_name="本地作者")

    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        with _captured(pg_engine) as seen:
            assert await repo.update_metadata_many([], {"author_id": 5}) == 0
            assert await repo.update_metadata_many([LOCAL_IDS[0]], {}) == 0

    writes = [
        q for q in seen
        if q.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
    ]
    assert writes == []
    assert not any("novel.id = " in q for q in seen), "不许逐行读"
    assert _row(session_factory, LOCAL_IDS[0]).author_id is None
