"""「我的上传」读路径 —— 列表 / 阈值 / 批量作用域一致性。

权威：``docs/LIBRARY_DESIGN.md`` §5（read path：书架 = 一次特殊搜索
``source:local;`` + 阈值归零）与 §7-4 / §7-6。

这里钉住的是**后端一侧**的契约：DSL 的 ``source:`` 字段由 ORM 列自动派生
（无需白名单改动）、``like >= min_like`` 在 ``min_like=0`` 时不加条件、
批量作用域（``BatchScope all_matched`` 的同一个解析函数）与列表查询命中同一
集合，以及 ``order_by='id'`` 倒序时最新上传排最前（§3.1 的 id 递增锚点）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import EpubStatus
from copixiv.core.services import QuerySpec, parse_search_keyword
from copixiv.db.models import Author, Novel
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.library.ingest import ingest_uploads
from copixiv.features.novels.batch_operations import resolve_batch_scope
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import check_epub

# 「我的上传」section 的参数（前端 SECTIONS 表：keyword + 归零阈值）
UPLOADS_KEYWORD = "source:local;"
UPLOADS_MIN_LIKE = 0
UPLOADS_MIN_TEXT = 0

# frontend/src/config.ts 里的默认阈值 —— 不归零书架就是空的（§5）
DEFAULT_MIN_LIKE = 500
DEFAULT_MIN_TEXT = 3000


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


def _upload_local(
    session_factory, download_dir: Path, name: str, body: str,
) -> int:
    with session_factory() as session:
        result = ingest_uploads(
            session,
            FileStorage(download_dir=str(download_dir)),
            [(name, body.encode("utf-8"))],
        )[0]
        session.commit()
    return result.novel_id


def _seed_pixiv_novel(
    session_factory, novel_id: int, *, like: int, text_chars: int,
) -> None:
    with session_factory() as session:
        session.add(Author(author_id=novel_id, author_name=f"作者{novel_id}"))
        session.flush()
        session.add(Novel(
            id=novel_id, title=f"pixiv 书{novel_id}", author_id=novel_id,
            author_name=f"作者{novel_id}", path=f"download/0000/{novel_id}.txt",
            like=like, text=text_chars, source="pixiv",
        ))
        session.commit()


def _uploads_spec(min_like: int, min_text: int, **kwargs) -> QuerySpec:
    return QuerySpec(
        conditions=parse_search_keyword(UPLOADS_KEYWORD),
        min_like=min_like, min_text=min_text, per_page=50, **kwargs,
    )


# ---------------------------------------------------------------------------
# §7-6：列表能查到本地行；阈值不归零则查不到
# ---------------------------------------------------------------------------


async def test_uploads_keyword_finds_local_rows(session_factory, tmp_path):
    local_id = _upload_local(session_factory, tmp_path / "download", "本地书.txt", "本地正文")
    _seed_pixiv_novel(session_factory, 1, like=5000, text_chars=8000)

    with session_factory() as session:
        found = await SQLAlchemyNovelRepository(session).get_novels(
            _uploads_spec(UPLOADS_MIN_LIKE, UPLOADS_MIN_TEXT)
        )

    assert [n.id for n in found["novels"]] == [local_id]
    assert found["novels"][0].source == "local"


async def test_thresholds_must_be_zeroed_for_the_shelf(session_factory, tmp_path):
    """§5：本地书 ``like=0``，默认阈值 500/3000 下书架是空的。"""
    _upload_local(session_factory, tmp_path / "download", "本地书.txt", "本地正文")

    with session_factory() as session:
        repo = SQLAlchemyNovelRepository(session)
        with_defaults = await repo.get_novels(
            _uploads_spec(DEFAULT_MIN_LIKE, DEFAULT_MIN_TEXT)
        )
        zeroed = await repo.get_novels(
            _uploads_spec(UPLOADS_MIN_LIKE, UPLOADS_MIN_TEXT)
        )
        # 只归零 min_like：min_text=3000 仍会把 text 短的本地书挡掉
        half_zeroed = await repo.get_novels(_uploads_spec(0, DEFAULT_MIN_TEXT))

    assert with_defaults["novels"] == []
    assert half_zeroed["novels"] == []
    assert len(zeroed["novels"]) == 1


async def test_source_field_is_a_derived_dsl_field(session_factory, tmp_path):
    """``source`` 由 ``Novel.__table__.c`` 派生，加列即自动可检索（§2）。"""
    _upload_local(session_factory, tmp_path / "download", "本地书.txt", "本地正文")
    _seed_pixiv_novel(session_factory, 1, like=5000, text_chars=8000)

    with session_factory() as session:
        repo = SQLAlchemyNovelRepository(session)
        assert "source" in repo.VALID_NOVEL_FIELDS
        pixiv = await repo.get_novels(QuerySpec(
            conditions=parse_search_keyword("source:pixiv;"),
            min_like=0, min_text=0, per_page=50,
        ))
    assert [n.id for n in pixiv["novels"]] == [1]

    # 白名单只放宽了真实存在的列：未知字段照旧 ValidationError（→ 400）
    with session_factory() as session:
        with pytest.raises(ValidationError):
            await SQLAlchemyNovelRepository(session).get_novels(QuerySpec(
                conditions=[("no_such_field", "x")],
                min_like=0, min_text=0, per_page=10,
            ))


# ---------------------------------------------------------------------------
# §7-6：批量作用域（BatchScope all_matched）与列表命中同一集合
# ---------------------------------------------------------------------------


async def test_batch_scope_all_matched_agrees_with_the_list(
    session_factory, tmp_path,
):
    download = tmp_path / "download"
    first = _upload_local(session_factory, download, "第一本.txt", "第一本正文")
    second = _upload_local(session_factory, download, "第二本.txt", "第二本正文")
    _seed_pixiv_novel(session_factory, 7, like=5000, text_chars=8000)

    with session_factory() as session:
        repo = SQLAlchemyNovelRepository(session)
        listed = await repo.get_novels(
            _uploads_spec(UPLOADS_MIN_LIKE, UPLOADS_MIN_TEXT)
        )
        scoped = await resolve_batch_scope(
            repo,
            mode="all_matched",
            novel_ids=[],
            keyword=UPLOADS_KEYWORD,
            min_like=UPLOADS_MIN_LIKE,
            min_text=UPLOADS_MIN_TEXT,
            excluded_ids=[],
        )

    assert sorted(scoped) == sorted(n.id for n in listed["novels"])
    assert sorted(scoped) == sorted([first, second])


async def test_order_by_id_desc_puts_the_newest_upload_first(
    session_factory, tmp_path,
):
    """§3.1：id 是负的时间坐标，服务端按 ``abs(id)`` 排序 → 最新上传在最前。"""
    download = tmp_path / "download"
    first = _upload_local(session_factory, download, "旧.txt", "旧正文")
    second = _upload_local(session_factory, download, "新.txt", "新正文")

    with session_factory() as session:
        found = await SQLAlchemyNovelRepository(session).get_novels(
            _uploads_spec(UPLOADS_MIN_LIKE, UPLOADS_MIN_TEXT, order_by="id")
        )

    assert [n.id for n in found["novels"]] == [second, first]


# ---------------------------------------------------------------------------
# §3.1：本地与 pixiv 在同一根时间轴上（混合列表落位 + 跨接缝翻页）
# ---------------------------------------------------------------------------


async def test_mixed_list_puts_local_on_the_same_time_axis(
    session_factory, tmp_path,
):
    """混合列表里本地书必须落在**水位之上**。

    这是「时间」按钮曾经排不对的根因：旧的 ``id = -(10**9)+n`` 让本地书恒在
    数轴负端（DESC 永远最后），与上传时间无关。id 换成负的时间坐标之后，
    ``ORDER BY abs(id)`` 才把两个来源放回同一根轴。
    """
    _seed_pixiv_novel(session_factory, 1_000_000, like=5000, text_chars=8000)
    _seed_pixiv_novel(session_factory, 2_000_000, like=5000, text_chars=8000)
    local_id = _upload_local(
        session_factory, tmp_path / "download", "本地书.txt", "本地正文",
    )
    assert abs(local_id) > 2_000_000  # 坐标 = 水位 + 1

    with session_factory() as session:
        repo = SQLAlchemyNovelRepository(session)
        desc = await repo.get_novels(QuerySpec(
            order_by="id", order_direction="DESC", min_like=0, min_text=0,
            per_page=10,
        ))
        asc = await repo.get_novels(QuerySpec(
            order_by="id", order_direction="ASC", min_like=0, min_text=0,
            per_page=10,
        ))

    assert [n.id for n in desc["novels"]] == [local_id, 2_000_000, 1_000_000]
    assert [n.id for n in asc["novels"]] == [1_000_000, 2_000_000, local_id]


async def test_keyset_pagination_crosses_the_local_pixiv_seam(
    session_factory, tmp_path,
):
    """跨「本地 / pixiv 接缝」翻页不重不漏。

    游标里只有身份 id，坐标由 ``abs`` 反推 —— 所以游标形状没变（§3.1）。
    """
    for novel_id in (1_000_000, 2_000_000, 3_000_000):
        _seed_pixiv_novel(session_factory, novel_id, like=5000, text_chars=8000)
    local_id = _upload_local(
        session_factory, tmp_path / "download", "本地书.txt", "本地正文",
    )

    seen: list[int] = []
    cursor = None
    with session_factory() as session:
        repo = SQLAlchemyNovelRepository(session)
        for _ in range(4):  # 页大小 2，最多两页就取完
            page = await repo.get_novels(QuerySpec(
                order_by="id", order_direction="DESC", min_like=0, min_text=0,
                per_page=2, cursor=cursor,
            ))
            seen.extend(n.id for n in page["novels"])
            cursor = page["cursor"]
            if cursor is None:
                break

    assert seen == [local_id, 3_000_000, 2_000_000, 1_000_000]


# ---------------------------------------------------------------------------
# §7-4：巡检跑过一轮后 has_epub 仍是 3（负 id 不在 id > cursor 区间）
# ---------------------------------------------------------------------------


async def test_local_row_has_epub_survives_check_epub(session_factory, tmp_path):
    local_id = _upload_local(
        session_factory, tmp_path / "download", "本地书.txt", "本地正文",
    )
    with session_factory() as session:
        assert session.execute(
            select(Novel.has_epub).where(Novel.id == local_id)
        ).scalar() == EpubStatus.NO_IMAGES == 3

    await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

    with session_factory() as session:
        row = session.get(Novel, local_id)
        assert row is not None
        assert row.has_epub == EpubStatus.NO_IMAGES == 3
        assert row.path is not None
