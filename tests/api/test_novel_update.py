"""元数据编辑端点测试 —— ``PATCH /api/novels/{novel_id}``。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §4（已拍板不做字段锁）/ §5（PATCH 三态与
校验表）。两条 2026-09-18 拍板在这里被钉住：

* ``title`` / ``series_name`` **仅本地书**可改 —— pixiv 书提交 → 400（不是静默
  忽略：静默忽略会让前端以为保存成功）；
* ``caption`` 全局不可编辑，与 ``source`` / ``path`` / ``id`` / ``has_epub`` 等
  结构性字段一样，在 schema 层被 ``extra="forbid"`` 拒掉 → 422（FastAPI 请求体
  校验的既有约定；业务规则拒绝才是 400）。

夹具与 ``tests/api/test_library_uploads.py`` 同构：最小 app + DomainError 处理器
+ app.state 依赖。清库由 ``clean_db`` 负责（PG 共享测试库）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text as sa_text

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.draft import build_from_novel_info
from copixiv.core.exceptions import DomainError
from copixiv.db.models import Author, Novel, Tag
from copixiv.features.novels import api as novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.storage.file_storage import FileStorage

LOCAL_ID = -999_000_001          # 本地书：负数锚点段（LIBRARY_DESIGN.md §3.1）
PIXIV_ID = 987_654
AUTHOR_ID = 555
USER_TIME = datetime(2019, 1, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


class _FakeTaskManager:
    def run_task(self, name: str, params: dict | None = None) -> int:
        return 1


@pytest.fixture
def client(session_factory, tmp_path):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = AppConfig()
    app.state.file_storage = FileStorage(download_dir=str(tmp_path / "download"))
    app.state.task_manager = _FakeTaskManager()

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 造数 / 读取
# ---------------------------------------------------------------------------

def _seed_local(session_factory, **over):
    """一行本地书：author_id 为 NULL、author_name 非 NULL、has_epub=3。"""
    values = dict(
        id=LOCAL_ID,
        title="本地原书名",
        author_id=None,
        author_name="未知作者",
        path=f"download/local/本地原书名_{LOCAL_ID}.txt",
        like=0, view=0, text=1234,
        create_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        has_epub=3,
        tags=["原有标签"],
        source="local",
        source_key="a" * 64,
    )
    values.update(over)
    with session_factory() as s:
        s.add(Novel(**values))
        s.commit()


def _seed_pixiv(session_factory, **over):
    values = dict(
        id=PIXIV_ID,
        title="pixiv 原标题",
        author_id=AUTHOR_ID,
        author_name="pixiv 原作者",
        path=f"download/0098/原标题_{PIXIV_ID}.txt",
        like=10, view=20, text=2000,
        caption="pixiv 原简介",
        series_id=None, series_name="pixiv 原系列", series_index=3,
        create_time=USER_TIME,
        has_epub=2,
        tags=["原有标签"],
        source="pixiv",
        source_key=None,
    )
    values.update(over)
    with session_factory() as s:
        s.add(Author(author_id=AUTHOR_ID, author_name="pixiv 原作者"))
        s.add(Novel(**values))
        s.commit()


def _row(session_factory, nid: int) -> Novel:
    with session_factory() as s:
        return s.get(Novel, nid)


def _patch(client, nid: int, payload: dict):
    return client.patch(f"/api/novels/{nid}", json=payload)


# ---------------------------------------------------------------------------
# ① 本地书：六个字段全改
# ---------------------------------------------------------------------------

def test_local_book_edits_all_six_fields(client, session_factory):
    _seed_local(session_factory)

    response = _patch(client, LOCAL_ID, {
        "title": "  新书名  ",            # strip 是服务端的事
        "author_name": "新作者",
        "series_name": "新系列",
        "series_index": 7,
        "tags": ["新标签A", "新标签B"],
        "create_time": "2020-05-05",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    # 响应是**更新后的完整对象**（前端据此原地替换卡片）
    assert body["id"] == LOCAL_ID
    assert body["title"] == "新书名"
    assert body["author_name"] == "新作者"
    assert body["series_name"] == "新系列"
    assert body["series_index"] == 7
    assert sorted(body["tags"]) == ["新标签a", "新标签b"]   # parse_tags 小写归一
    assert body["source"] == "local"
    assert body["text"] == 1234                              # 机器事实不变
    assert body["has_epub"] == 3

    row = _row(session_factory, LOCAL_ID)
    assert row.title == "新书名" and row.author_name == "新作者"
    assert row.series_name == "新系列" and row.series_index == 7
    # 裸日期按 Asia/Tokyo 零点解析（parse_pixiv_time 的既有约定）→ 比较**时刻**
    assert row.create_time == datetime(2020, 5, 4, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# ② pixiv 书：title / series_name 只读；其余四个可改
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"title": "想改标题"},
    {"series_name": "想改系列"},
])
def test_pixiv_book_rejects_local_only_fields(client, session_factory, payload):
    """服务端执法：不是静默忽略，而是 400 + 说明。"""
    _seed_pixiv(session_factory)

    response = _patch(client, PIXIV_ID, payload)

    assert response.status_code == 400, response.text
    assert "仅本地书可编辑" in response.json()["detail"]
    # 硬拒 = 一个字都没写
    row = _row(session_factory, PIXIV_ID)
    assert row.title == "pixiv 原标题"
    assert row.series_name == "pixiv 原系列"


def test_pixiv_book_edits_the_other_four_fields(client, session_factory):
    _seed_pixiv(session_factory)

    response = _patch(client, PIXIV_ID, {
        "author_name": "我给作者起的别名",
        "series_index": 9,
        "tags": ["新标签"],
        "create_time": "2021-03-04",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["author_name"] == "我给作者起的别名"
    assert body["series_index"] == 9
    # 编辑器写回是**整组替换**（所见即所得）——与来源刷新的"只增不减"并集
    # （_upsert_rows 第 4 步）刻意不同：用户删掉一个标签，在编辑器里就该消失。
    assert body["tags"] == ["新标签"]
    assert body["title"] == "pixiv 原标题"      # 未提交 → 不动


# ---------------------------------------------------------------------------
# ③ 不可编辑字段：schema 层拒（422）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"caption": "想改简介"},
    {"source": "local"},
    {"source_key": "b" * 64},
    {"path": "download/x.txt"},
    {"id": 1},
    {"has_epub": 1},
    {"like": 999},
    {"unknown_field": "x"},
])
def test_non_editable_fields_are_rejected(client, session_factory, payload):
    _seed_local(session_factory)

    response = _patch(client, LOCAL_ID, payload)

    assert response.status_code == 422, response.text
    # 本地书的行没被动过
    assert _row(session_factory, LOCAL_ID).title == "本地原书名"


def test_patch_without_any_field_is_rejected(client, session_factory):
    _seed_local(session_factory)

    response = _patch(client, LOCAL_ID, {})

    assert response.status_code == 400
    assert "没有要更新的字段" in response.json()["detail"]


# ---------------------------------------------------------------------------
# ④ 单字段语义：空白 / 归一 / 清空
# ---------------------------------------------------------------------------

def test_blank_title_is_rejected(client, session_factory):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"title": "   "}).status_code == 400
    assert _patch(client, LOCAL_ID, {"title": None}).status_code == 400
    assert _row(session_factory, LOCAL_ID).title == "本地原书名"


def test_author_name_never_becomes_null(client, session_factory):
    """清空 → 「未知作者」，绝不落 NULL（本地书硬不变量 + sync_empty_name 隔离）。"""
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"author_name": ""}).status_code == 200
    assert _row(session_factory, LOCAL_ID).author_name == "未知作者"

    assert _patch(client, LOCAL_ID, {"author_name": None}).status_code == 200
    assert _row(session_factory, LOCAL_ID).author_name == "未知作者"


def test_series_and_date_can_be_cleared_with_null(client, session_factory):
    _seed_local(session_factory, series_name="某系列", series_index=2)

    response = _patch(client, LOCAL_ID, {
        "series_name": None, "series_index": None, "create_time": None,
    })

    assert response.status_code == 200, response.text
    row = _row(session_factory, LOCAL_ID)
    assert row.series_name is None
    assert row.series_index is None
    assert row.create_time is None


@pytest.mark.parametrize("payload,expected", [
    ({"series_index": -1}, 400),      # 业务规则 → ValidationError → 400
    ({"series_index": 3.5}, 422),     # Pydantic 整数校验 → 422
])
def test_series_index_must_be_a_non_negative_int(
    client, session_factory, payload, expected,
):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, payload).status_code == expected
    assert _row(session_factory, LOCAL_ID).series_index is None


def test_series_index_helper_guards_non_int_types():
    """用例层的类型防御：Pydantic 的 lax 模式会把 ``True`` 收成 1、``"3"`` 收成 3，
    直接调用用例（非 HTTP 路径）时这一层不该放行。"""
    from copixiv.core.exceptions import ValidationError
    from copixiv.features.novels.update_metadata import _series_index

    with pytest.raises(ValidationError):
        _series_index(True)
    with pytest.raises(ValidationError):
        _series_index("3")
    assert _series_index(None) is None
    assert _series_index(0) == 0


def test_title_length_is_capped(client, session_factory):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"title": "x" * 301}).status_code == 400
    assert _patch(client, LOCAL_ID, {"title": "x" * 300}).status_code == 200


# ---------------------------------------------------------------------------
# ⑤ tags：与抓取路径同源归一 + 触发器维护参考计数
# ---------------------------------------------------------------------------

async def test_tags_are_normalised_like_the_scrape_path(client, session_factory):
    """``parse_tags`` 归一（小写 / 去括号 / 按 ``/|#、\\`` 切分）+ 别名映射。"""
    _seed_local(session_factory)
    with session_factory() as s:
        await SQLAlchemyTagRepository(s).create_alias(
            {"source": "旧名", "target": "新名"},
        )
        s.commit()

    response = _patch(client, LOCAL_ID, {
        "tags": ["R-18", "魔法/少女", "标签(备注)", "旧名", "r-18"],
    })

    assert response.status_code == 200, response.text
    # 小写化、按 / 切开、去 ASCII 括号、别名 旧名→新名、重复项合并
    assert set(response.json()["tags"]) == {"r-18", "魔法", "少女", "标签备注", "新名"}
    assert response.json()["tags"] == sorted(response.json()["tags"])

    # tag 行与 reference_count 与 novel.tags 的真值逐项一致（语句级触发器）
    with session_factory() as s:
        truth = dict(s.execute(sa_text(
            "SELECT t.tag_name, count(*) FROM novel n, "
            "unnest(n.tags) AS t(tag_name) GROUP BY 1"
        )).all())
        stored = dict(s.execute(select(Tag.name, Tag.reference_count)).all())
    assert truth
    assert {name: stored.get(name) for name in truth} == truth
    # 别名两端都建了 tag 行；写入的是目标名
    assert "新名" in stored


def test_too_many_or_too_long_tags_are_rejected(client, session_factory):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"tags": [f"t{i}" for i in range(51)]}).status_code == 400
    assert _patch(client, LOCAL_ID, {"tags": ["x" * 101]}).status_code == 400


# ---------------------------------------------------------------------------
# ⑥ 回归不变量：改过的字段不被随后的来源刷新顶掉
# ---------------------------------------------------------------------------

def _novel_info(**over):
    """榜单/搜索/关注流/作者目录 的 novelInfo 形状（来源刷新的输入）。"""
    data = dict(
        id=PIXIV_ID,
        title="来源新标题",
        caption="来源新简介",
        create_date="2020-05-05 12:00:00",
        total_bookmarks=999,
        total_view=888,
        text_length=555,
        tags=["来源新标签"],
        user=SimpleNamespace(id=AUTHOR_ID, name="来源作者名"),
        series=SimpleNamespace(id=777, title="来源系列名", index=1),
    )
    data.update(over)
    return SimpleNamespace(**data)


async def test_user_edits_survive_a_source_refresh(client, session_factory):
    """前置工作（b76e8cd）的意义所在：结构性安全的字段改过就顶不掉。"""
    _seed_pixiv(session_factory)
    assert _patch(client, PIXIV_ID, {
        "author_name": "我改的作者名",
        "series_index": 42,
        "create_time": "2021-03-04",
        "tags": ["我加的标签"],
    }).status_code == 200

    with session_factory() as s:
        await SQLAlchemyNovelRepository(s).upsert_novels(
            [build_from_novel_info(_novel_info())],
        )
        s.commit()

    row = _row(session_factory, PIXIV_ID)
    # 非空者胜 / 并集 / 退出覆盖白名单 —— 用户的值全部留存
    assert row.author_name == "我改的作者名"
    assert row.series_index == 42
    assert row.create_time == datetime(2021, 3, 3, 15, 0, tzinfo=timezone.utc)
    assert "我加的标签" in row.tags
    # 对照：来源权威的字段照旧被来源覆盖（title / series_name / caption / 统计）
    assert row.title == "来源新标题"
    assert row.series_name == "来源系列名"
    assert (row.like, row.view, row.text) == (999, 888, 555)


# ---------------------------------------------------------------------------
# ⑦ 本地书不变量：结构性列编辑前后不变
# ---------------------------------------------------------------------------

def test_local_book_structural_columns_are_untouched(client, session_factory):
    _seed_local(session_factory)
    before = _row(session_factory, LOCAL_ID)

    assert _patch(client, LOCAL_ID, {
        "title": "改了标题", "author_name": "改了作者", "tags": ["x"],
    }).status_code == 200

    after = _row(session_factory, LOCAL_ID)
    assert after.source == "local"
    assert after.source_key == before.source_key
    assert after.has_epub == before.has_epub == 3
    assert after.path == before.path            # 改标题不移动文件（§10-6 拍板）
    assert after.id == before.id
    assert after.author_id is None              # 文本作者名不产生 Author 实体


# ---------------------------------------------------------------------------
# ⑧ 404
# ---------------------------------------------------------------------------

def test_unknown_novel_is_404(client, session_factory):
    response = _patch(client, 123_456_789, {"author_name": "x"})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
