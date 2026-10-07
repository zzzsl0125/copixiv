"""元数据编辑端点测试 —— ``PATCH /api/novels/{novel_id}``。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §4（已拍板不做字段锁）/ §5（PATCH 三态与
校验表）。五条拍板在这里被钉住：

* ``title`` / ``author_id`` / ``series_id`` **仅本地书**可改（2026-09-22 起三个
  都收掉）—— pixiv 书提交 → 400（不是静默忽略：静默忽略会让前端以为保存成功）；
  ``series_name`` **谁都不能改**，提交即 422（名字是 id 的解析结果）；
* 可编辑清单由后端下发（``NovelBase.editable_fields``，唯一所有者在
  ``features/novels/field_policy.py``）：前端不再自己判断 ``source``；
* ``caption`` 全局不可编辑，与 ``source`` / ``path`` / ``id`` / ``has_epub`` 等
  结构性字段一样，在 schema 层被 ``extra="forbid"`` 拒掉 → 422（FastAPI 请求体
  校验的既有约定；业务规则拒绝才是 400）；
* **作者编辑 ``author_id``**（2026-09-19）：``author_name`` 一起被 422 挡掉，
  名字是 id 的解析结果——本地 ``author`` 表优先、pixiv 兜底、两者都拿不到就落
  NULL 等 ``sync_empty_name`` 补。本文件的 pixiv 兜底由 ``_FakePixivClient``
  提供，``pixiv.calls`` 用来断言"本地命中不打网络"；
* **解除关联的收尾两者不同**（2026-09-22）：``author_id = null`` 保留显示名
  （名字有独立来源=文件名解析），``series_id = null`` 连名字与章节号一起清
  （本地书的系列名只可能由 id 解析而来，留着就是用户清不掉的孤儿显示名）。

pixiv 书那三列错配之后的出口是**同步**（``POST /api/novels/{id}/sync``）——
端到端行为在 ``tests/api/test_novel_sync.py``。

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
from copixiv.db.models import Author, Novel, Series, Tag
from copixiv.features.novels import api as novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.storage.file_storage import FileStorage

LOCAL_ID = -999_000_001          # 本地书：负数锚点段（LIBRARY_DESIGN.md §3.1）
PIXIV_ID = 987_654
AUTHOR_ID = 555                  # pixiv 书自带的作者（本地 author 行有名字）
REMOTE_AUTHOR_ID = 777_777       # 本地表没有、只能问 Pixiv 的作者
SERIES_ID = 606                  # pixiv 书自带的系列（本地 series 行有名字）
REMOTE_SERIES_ID = 888_888       # 本地表没有、只能问 Pixiv 的系列
USER_TIME = datetime(2019, 1, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


class _FakeTaskManager:
    def run_task(self, name: str, params: dict | None = None) -> int:
        return 1


class _FakePixivClient:
    """``user_detail`` / ``novel_series`` 替身：名字解析的 pixiv 兜底在这里被观测。

    ``calls`` / ``series_calls`` 让测试能断言"本地表命中就不打网络"，``fail``
    模拟 pixiv 不可达（冷却 / 限流 / 网络故障）——那条路径必须是"能存，只是名字
    待解析"。
    """

    def __init__(
        self,
        names: dict[int, str] | None = None,
        series_titles: dict[int, str] | None = None,
    ):
        self.names = {REMOTE_AUTHOR_ID: "远端作者"} if names is None else names
        self.series_titles = (
            {REMOTE_SERIES_ID: "远端系列"} if series_titles is None else series_titles
        )
        self.calls: list[int] = []
        self.series_calls: list[int] = []
        self.fail = False

    async def user_detail(self, user_id: int) -> dict:
        self.calls.append(user_id)
        if self.fail:
            raise RuntimeError("pixiv unavailable")
        return {"user": {"id": user_id, "name": self.names.get(user_id, "")}}

    async def novel_series(self, series_id: int, fetch_all: bool = False) -> dict:
        self.series_calls.append(series_id)
        if self.fail:
            raise RuntimeError("pixiv unavailable")
        title = self.series_titles.get(series_id)
        # 真实形状：``/v2/novel/series`` 返回裸的 ``{novels: [...]}``，
        # 系列名挂在小说的 ``series.title`` 上（见 resolve_series.py）。
        novels = (
            [{"id": 1, "series": {"id": series_id, "title": title}}] if title else []
        )
        return {"novels": novels, "next_url": None}


@pytest.fixture
def pixiv():
    return _FakePixivClient()


@pytest.fixture
def client(session_factory, tmp_path, pixiv):
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
    app.state.client = pixiv

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
        # series_id 有外键：造一行带 id 的小说就必须造系列行。名字**故意留空**，
        # 模拟抓取的真实顺序（先 ensure_exists 占位，名字由 update_summary 推导）。
        if values.get("series_id") is not None:
            s.add(Series(series_id=values["series_id"]))
        s.add(Novel(**values))
        s.commit()


def _row(session_factory, nid: int) -> Novel:
    with session_factory() as s:
        return s.get(Novel, nid)


def _author(session_factory, author_id: int):
    with session_factory() as s:
        return s.get(Author, author_id)


def _series(session_factory, series_id: int):
    with session_factory() as s:
        return s.get(Series, series_id)


def _patch(client, nid: int, payload: dict):
    return client.patch(f"/api/novels/{nid}", json=payload)


# ---------------------------------------------------------------------------
# ① 本地书：七个字段全改
# ---------------------------------------------------------------------------

def test_local_book_edits_all_seven_fields(client, session_factory):
    _seed_local(session_factory)

    response = _patch(client, LOCAL_ID, {
        "title": "  新书名  ",            # strip 是服务端的事
        "author_id": REMOTE_AUTHOR_ID,    # 作者是 id：名字由它解析出来
        "series_id": REMOTE_SERIES_ID,    # 系列同理
        "series_index": 7,
        "tags": ["新标签A", "新标签B"],
        "create_time": "2020-05-05",
        "caption": "  自己写的简介  ",     # 仅本地书（2026-09-22）
    })

    assert response.status_code == 200, response.text
    body = response.json()
    # 响应是**更新后的完整对象**（前端据此原地替换卡片）
    assert body["id"] == LOCAL_ID
    assert body["title"] == "新书名"
    assert body["author_id"] == REMOTE_AUTHOR_ID
    assert body["author_name"] == "远端作者"     # 由 id 解析，不接受客户端直接写
    assert body["series_id"] == REMOTE_SERIES_ID
    assert body["series_name"] == "远端系列"
    assert body["series_index"] == 7
    assert sorted(body["tags"]) == ["新标签a", "新标签b"]   # parse_tags 小写归一
    assert body["caption"] == "自己写的简介"                 # strip 同 title
    assert body["source"] == "local"
    assert body["text"] == 1234                              # 机器事实不变
    assert body["has_epub"] == 3

    row = _row(session_factory, LOCAL_ID)
    assert row.title == "新书名"
    assert row.author_id == REMOTE_AUTHOR_ID and row.author_name == "远端作者"
    assert row.series_id == REMOTE_SERIES_ID and row.series_name == "远端系列"
    assert row.series_index == 7
    assert row.caption == "自己写的简介"
    # 裸日期按 Asia/Tokyo 零点解析（parse_pixiv_time 的既有约定）→ 比较**时刻**
    assert row.create_time == datetime(2020, 5, 4, 15, 0, tzinfo=timezone.utc)

    # 这就是本功能的全部意义：本地书真的挂到了 pixiv 作者/系列名下——
    # author / series 行存在（可追更、可跳系列页），且带着同一个名字。
    author = _author(session_factory, REMOTE_AUTHOR_ID)
    assert author is not None and author.author_name == "远端作者"
    series = _series(session_factory, REMOTE_SERIES_ID)
    assert series is not None and series.series_name == "远端系列"


# ---------------------------------------------------------------------------
# ② pixiv 书：title / author_id / series_id / caption 只读；其余三个可改
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"title": "想改标题"},
    {"author_id": 777},
    {"series_id": 777},
    {"caption": "想改简介"},
])
def test_pixiv_book_rejects_local_only_fields(client, session_factory, payload):
    """服务端执法：不是静默忽略，而是 400 + 说明。

    四个字段一起收，理由分两种：``title`` / ``series_id`` / ``caption`` 会被来源
    刷新顶回去（``caption`` 与 ``series_name`` 都在覆盖白名单里，改 id 只会造出
    "id 指向新系列、名字还是旧的"），``author_id`` 则压根不会被覆盖——它会
    **永久**地让「作者是谁」与来源不符。修正入口是同步端点，不是手改。
    """
    _seed_pixiv(session_factory)

    response = _patch(client, PIXIV_ID, payload)

    assert response.status_code == 400, response.text
    assert "仅本地书可编辑" in response.json()["detail"]
    # 硬拒 = 一个字都没写
    row = _row(session_factory, PIXIV_ID)
    assert row.title == "pixiv 原标题"
    assert row.author_id == AUTHOR_ID
    assert row.series_name == "pixiv 原系列"
    assert row.series_id is None
    assert row.caption == "pixiv 原简介"


def test_pixiv_book_rejections_name_the_field(client, session_factory):
    """400 文案按字段给（集中声明在 field_policy.py），前端直接展示 detail。"""
    from copixiv.features.novels import field_policy

    _seed_pixiv(session_factory)

    assert _patch(client, PIXIV_ID, {"author_id": 777}).json()["detail"] == (
        field_policy.rejection_reason("author_id")
    )
    assert _patch(client, PIXIV_ID, {"title": "x"}).json()["detail"] == (
        field_policy.rejection_reason("title")
    )
    assert _patch(client, PIXIV_ID, {"caption": "x"}).json()["detail"] == (
        field_policy.rejection_reason("caption")
    )
    # caption 走的是"来源会覆盖"那条模板（与 author_id 的单独一句不同）
    assert "仅本地书可编辑" in field_policy.rejection_reason("caption")
    # 同一个请求提交多个非法字段 → 报清单里最靠前的那个（顺序是契约）
    assert _patch(
        client, PIXIV_ID, {"caption": "x", "series_id": 777, "title": "x", "author_id": 777},
    ).json()["detail"] == field_policy.rejection_reason("title")


def test_pixiv_book_edits_the_other_three_fields(client, session_factory):
    _seed_pixiv(session_factory)

    response = _patch(client, PIXIV_ID, {
        "series_index": 9,
        "tags": ["新标签"],
        "create_time": "2021-03-04",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    # 作者身份是来源的：pixiv 书连提交都不许，未提交时更是一个字节没动。
    assert body["author_id"] == AUTHOR_ID
    assert body["author_name"] == "pixiv 原作者"
    assert body["series_index"] == 9
    # 编辑器写回是**整组替换**（所见即所得）——与来源刷新的"只增不减"并集
    # （_upsert_rows 第 4 步）刻意不同：用户删掉一个标签，在编辑器里就该消失。
    assert body["tags"] == ["新标签"]
    assert body["title"] == "pixiv 原标题"      # 未提交 → 不动


# ---------------------------------------------------------------------------
# ②-b 可编辑清单由后端下发：NovelBase.editable_fields
# ---------------------------------------------------------------------------

LOCAL_EDITABLE = [
    "title", "author_id", "series_id", "series_index", "tags", "create_time",
    "caption",
]
PIXIV_EDITABLE = ["series_index", "tags", "create_time"]


def test_editable_fields_are_published_for_local_books(client, session_factory):
    """本地书：列表响应与 PATCH 响应都带**七个**字段。

    这是"字段策略的单一所有者"的落地形状：前端不再自己判断 ``source``，
    而是照服务端下发的清单渲染（同一份策略在 ``field_policy.py``）。
    """
    _seed_local(session_factory)

    listed = client.get("/api/novels/", params={"order_by": "id"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["novels"][0]["editable_fields"] == LOCAL_EDITABLE

    patched = _patch(client, LOCAL_ID, {"tags": ["x"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["editable_fields"] == LOCAL_EDITABLE


def test_editable_fields_are_published_for_pixiv_books(client, session_factory):
    """pixiv 书：三个字段——title / author_id / series_id / caption 不在清单里。"""
    _seed_pixiv(session_factory)

    listed = client.get("/api/novels/", params={"order_by": "id"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["novels"][0]["editable_fields"] == PIXIV_EDITABLE

    patched = _patch(client, PIXIV_ID, {"series_index": 2})
    assert patched.status_code == 200, patched.text
    assert patched.json()["editable_fields"] == PIXIV_EDITABLE


def test_field_policy_is_derived_and_unknown_sources_are_conservative():
    """清单是**派生**的，且未知来源按 pixiv（最保守）——两条都别有第二份实现。"""
    from copixiv.features.library import local_axis
    from copixiv.features.novels import field_policy

    # 来源常量复用 local_axis，本模块不再造字符串字面量
    assert field_policy.SOURCE_LOCAL is local_axis.SOURCE_LOCAL
    assert field_policy.SOURCE_PIXIV is local_axis.SOURCE_PIXIV
    # "仅本地书可编辑" 从表派生（local 有、pixiv 没有），顺序即 400 报错顺序
    assert field_policy.LOCAL_ONLY_FIELDS == tuple(
        field for field in field_policy.EDITABLE_FIELDS[field_policy.SOURCE_LOCAL]
        if field not in field_policy.EDITABLE_FIELDS[field_policy.SOURCE_PIXIV]
    )
    assert field_policy.LOCAL_ONLY_FIELDS == (
        "title", "author_id", "series_id", "caption",
    )
    # 未知来源 = 最保守：来源没认领的行不该被手改
    assert list(field_policy.editable_fields(None)) == PIXIV_EDITABLE
    assert list(field_policy.editable_fields("sqlite-import")) == PIXIV_EDITABLE
    assert not field_policy.is_editable("pixiv", "title")
    assert field_policy.is_editable("local", "title")
    # caption 2026-09-22 从"全局不可编辑"改成"仅本地书"：未知来源按 pixiv 处理，
    # 所以它必须同时出现在两端（local 有、pixiv 没有）——这就是派生规则本身。
    assert field_policy.is_editable("local", "caption")
    assert not field_policy.is_editable("pixiv", "caption")
    assert not field_policy.is_editable(None, "caption")


# ---------------------------------------------------------------------------
# ③ 不可编辑字段：schema 层拒（422）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"source": "local"},
    {"source_key": "b" * 64},
    {"path": "download/x.txt"},
    {"id": 1},
    {"has_epub": 1},
    {"like": 999},
    {"unknown_field": "x"},
    # 名字自 2026-09-19 起都不可编辑：身份是 author_id / series_id，名字是解析结果。
    {"author_name": "我想直接改名字"},
    {"series_name": "我想直接改系列名"},
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


def test_author_id_resolves_from_the_local_table_without_asking_pixiv(
    client, session_factory, pixiv,
):
    """库里已知的名字不打网络——这条是"离线也能编辑"的保证。"""
    _seed_pixiv(session_factory)          # 建了 author 555 =「pixiv 原作者」
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"author_id": AUTHOR_ID}).status_code == 200

    assert _row(session_factory, LOCAL_ID).author_name == "pixiv 原作者"
    assert pixiv.calls == []


def test_author_name_is_null_when_pixiv_cannot_answer(
    client, session_factory, pixiv,
):
    """解析不出名字**不是失败**：id 照写，名字留 NULL 等 sync_empty_name 补。

    这条同时钉住本地书不变量的唯一例外——"author_name 非 NULL"只在
    ``author_id IS NULL`` 时必需（那个任务扫 NULL 名再拿 author_id 去 Pixiv，
    id 为 NULL 的行进去才会出事）。
    """
    _seed_local(session_factory)
    pixiv.fail = True

    response = _patch(client, LOCAL_ID, {"author_id": REMOTE_AUTHOR_ID})

    assert response.status_code == 200, response.text
    row = _row(session_factory, LOCAL_ID)
    assert row.author_id == REMOTE_AUTHOR_ID
    assert row.author_name is None
    # 占位 author 行仍要建（外键 + 追更按钮），只是名字待解析
    assert _author(session_factory, REMOTE_AUTHOR_ID) is not None


def test_clearing_author_id_keeps_the_display_name(client, session_factory):
    """解除关联只清 id：本地书的名字来自文件名解析，顺手清掉等于凭空丢信息。

    **与系列刻意不同**（2026-09-22）：``series_id = null`` 会连名字与章节号一起清
    ——本地书的系列名没有独立来源，见
    ``test_clearing_series_id_clears_name_and_index``。两个字段共用一套前端交互，
    收尾却不同，所以两条测试都得在，免得将来被"统一"掉。
    """
    _seed_local(session_factory)
    assert _patch(client, LOCAL_ID, {"author_id": REMOTE_AUTHOR_ID}).status_code == 200

    assert _patch(client, LOCAL_ID, {"author_id": None}).status_code == 200

    row = _row(session_factory, LOCAL_ID)
    assert row.author_id is None
    assert row.author_name == "远端作者"      # 名字留着


def test_clearing_author_id_on_a_nameless_row_restores_unknown_author(
    client, session_factory, pixiv,
):
    """``author_id`` 与 ``author_name`` 不许同时为 NULL。

    名字为 NULL 是"上一轮解析失败"的中间态，合法只在挂了 id 时；解除关联
    必须把它补回「未知作者」——否则这一行会掉进 ``sync_empty_name`` 的扫描
    集合，而那个任务假设每行都有 id 可用（LIBRARY_DESIGN.md §3.3）。
    """
    _seed_local(session_factory)
    pixiv.fail = True
    assert _patch(client, LOCAL_ID, {"author_id": REMOTE_AUTHOR_ID}).status_code == 200
    assert _row(session_factory, LOCAL_ID).author_name is None     # 待解析

    assert _patch(client, LOCAL_ID, {"author_id": None}).status_code == 200

    row = _row(session_factory, LOCAL_ID)
    assert row.author_id is None
    assert row.author_name == "未知作者"


@pytest.mark.parametrize("payload,expected", [
    ({"author_id": 0}, 400),          # 业务规则：pixiv 用户 id 从 1 开始
    ({"author_id": -5}, 400),
    ({"author_id": True}, 422),       # JSON true 不能变成"1 号作者"
    ({"author_id": 3.5}, 422),
    ({"author_id": "abc"}, 422),
])
def test_author_id_must_be_a_positive_int(
    client, session_factory, payload, expected,
):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, payload).status_code == expected
    assert _row(session_factory, LOCAL_ID).author_id is None


def test_author_name_writeback_normalises_the_author_row_and_siblings(
    client, session_factory,
):
    """名字是作者的**属性**：写回 author 行，并把同作者的陈旧名字一并规范。

    没有这条，本地书挂上去之后作者页显示的还是旧名，而卡片显示新名——同一
    作者两个名字。
    """
    _seed_pixiv(session_factory)                       # author 555 = 新名字
    with session_factory() as s:                       # 同作者的陈旧副本
        s.add(Novel(
            id=987_655, title="同作者旧书", author_id=AUTHOR_ID,
            author_name="过时的名字", path="download/0098/旧书_987655.txt",
            like=0, view=0, text=10, has_epub=3, tags=[], source="pixiv",
        ))
        s.commit()
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"author_id": AUTHOR_ID}).status_code == 200

    assert _author(session_factory, AUTHOR_ID).author_name == "pixiv 原作者"
    assert _row(session_factory, 987_655).author_name == "pixiv 原作者"


# ---------------------------------------------------------------------------
# ④-b 作者 / 系列 ID 预览（GET /api/novels/{author,series}/{id}）
# ---------------------------------------------------------------------------

def _lookup(client, author_id: int):
    return client.get(f"/api/novels/author/{author_id}")


def _lookup_series(client, series_id: int):
    return client.get(f"/api/novels/series/{series_id}")


def test_lookup_reads_the_local_name_and_counts_novels(client, session_factory):
    _seed_pixiv(session_factory)          # author 555 名下 1 本

    response = _lookup(client, AUTHOR_ID)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "author_id": AUTHOR_ID,
        "author_name": "pixiv 原作者",
        "source": "local",
        "novel_count": 1,
    }


def test_lookup_falls_back_to_pixiv_without_writing(client, session_factory, pixiv):
    """预览是只读的：查到了名字也不落库（落库是 PATCH 的事）。"""
    response = _lookup(client, REMOTE_AUTHOR_ID)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "author_id": REMOTE_AUTHOR_ID,
        "author_name": "远端作者",
        "source": "pixiv",
        "novel_count": 0,
    }
    assert pixiv.calls == [REMOTE_AUTHOR_ID]
    assert _author(session_factory, REMOTE_AUTHOR_ID) is None


def test_lookup_reports_unknown_instead_of_failing(client, session_factory, pixiv):
    """pixiv 不可达 → 200 + unknown；编辑器据此提示但**不禁止保存**。"""
    pixiv.fail = True

    response = _lookup(client, REMOTE_AUTHOR_ID)

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "unknown"
    assert response.json()["author_name"] is None


# ---------------------------------------------------------------------------
# ④-c 系列是 id 而不是名字（2026-09-19，与作者同构）
# ---------------------------------------------------------------------------

def test_series_id_resolves_from_the_local_table_without_asking_pixiv(
    client, session_factory, pixiv,
):
    """系列行有名字 → 不打网络。"""
    with session_factory() as s:
        s.add(Series(series_id=SERIES_ID, series_name="库里已知的系列"))
        s.commit()
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"series_id": SERIES_ID}).status_code == 200

    assert _row(session_factory, LOCAL_ID).series_name == "库里已知的系列"
    assert pixiv.series_calls == []


def test_series_name_falls_back_to_the_sibling_novels(
    client, session_factory, pixiv,
):
    """系列行还没名字（占位行）→ 用同系列小说行已有的名字，仍然不打网络。

    这条链不是可有可无的：``series`` 行的名字由 ``update_summary`` 从小说行
    推导（``max(novel.series_name)``），占位行在被推导之前一直是 NULL。
    """
    _seed_pixiv(session_factory, series_id=SERIES_ID)   # 小说行带着系列名
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"series_id": SERIES_ID}).status_code == 200

    assert _row(session_factory, LOCAL_ID).series_name == "pixiv 原系列"
    assert pixiv.series_calls == []


def test_series_name_falls_back_to_pixiv(client, session_factory, pixiv):
    """两边都没有 → 问一次 pixiv，并把名字写进 series 行。"""
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"series_id": REMOTE_SERIES_ID}).status_code == 200

    assert pixiv.series_calls == [REMOTE_SERIES_ID]
    assert _row(session_factory, LOCAL_ID).series_name == "远端系列"
    series = _series(session_factory, REMOTE_SERIES_ID)
    assert series is not None and series.series_name == "远端系列"


def test_series_name_is_null_when_nothing_can_resolve_it(
    client, session_factory, pixiv,
):
    """解析不出名字不是失败：id 照写，名字留 NULL 待 ``update_summary`` 补。"""
    _seed_local(session_factory)
    pixiv.fail = True

    response = _patch(client, LOCAL_ID, {"series_id": REMOTE_SERIES_ID})

    assert response.status_code == 200, response.text
    row = _row(session_factory, LOCAL_ID)
    assert row.series_id == REMOTE_SERIES_ID
    assert row.series_name is None
    # 占位 series 行仍要建（外键 + 系列页跳转），只是名字待解析
    assert _series(session_factory, REMOTE_SERIES_ID) is not None


def test_clearing_series_id_clears_name_and_index(client, session_factory):
    """解除系列关联清掉整个"系列三元组"（2026-09-22 修订）。

    名字**不能**像作者那样留着：``author_name`` 上传时来自文件名解析，有独立
    来源；本地书的 ``series_name`` 上传时是 NULL，只可能由 ``series_id`` 解析
    而来。留着它就只剩卡片上一条点不动的「系列：X」，而 ``series_name`` 提交
    即 422（名字不可编辑）——用户没有任何出口能清掉它。

    章节号同理：脱离系列之后"第几本"没有指代对象。本地书的这一列不会被
    ``fix_series_index`` 补回（它只扫 ``series_id`` 非空的行）。
    """
    _seed_local(session_factory)
    assert _patch(
        client, LOCAL_ID, {"series_id": REMOTE_SERIES_ID, "series_index": 5},
    ).status_code == 200
    linked = _row(session_factory, LOCAL_ID)
    assert (linked.series_id, linked.series_name, linked.series_index) == (
        REMOTE_SERIES_ID, "远端系列", 5,
    )

    response = _patch(client, LOCAL_ID, {"series_id": None})

    assert response.status_code == 200, response.text
    # 响应就是卡片原地替换用的那份数据：三个字段都必须已经是清空态
    body = response.json()
    assert body["series_id"] is None
    assert body["series_name"] is None
    assert body["series_index"] is None

    row = _row(session_factory, LOCAL_ID)
    assert row.series_id is None
    assert row.series_name is None
    assert row.series_index is None
    # 系列行本身不受影响：名字是 series 行的，与"这本书挂不挂它"无关
    assert _series(session_factory, REMOTE_SERIES_ID) is not None


def test_unlink_wins_over_an_explicit_series_index(client, session_factory):
    """显式章节号与解除关联同时提交 → 以解除为准（没有系列就没有"第几本"）。

    这条是**契约**不是巧合：前端 diff 只在值变化时提交字段，所以"清空系列 ID +
    改章节号"这种自相矛盾的组合确实可能到达服务端；章节号没有指代对象时，
    静默留一个孤儿比以解除为准更糟。
    """
    _seed_local(session_factory)
    assert _patch(client, LOCAL_ID, {"series_id": REMOTE_SERIES_ID}).status_code == 200

    assert _patch(
        client, LOCAL_ID, {"series_id": None, "series_index": 7},
    ).status_code == 200

    row = _row(session_factory, LOCAL_ID)
    assert row.series_id is None
    assert row.series_index is None



@pytest.mark.parametrize("payload,expected", [
    ({"series_id": 0}, 400),
    ({"series_id": -5}, 400),
    ({"series_id": True}, 422),       # JSON true 不能变成"1 号系列"
    ({"series_id": 3.5}, 422),
    ({"series_id": "abc"}, 422),
])
def test_series_id_must_be_a_positive_int(
    client, session_factory, payload, expected,
):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, payload).status_code == expected
    assert _row(session_factory, LOCAL_ID).series_id is None


def test_lookup_series_reads_local_names_and_counts_novels(client, session_factory):
    _seed_pixiv(session_factory, series_id=SERIES_ID)   # series_id 名下有 1 本

    response = _lookup_series(client, SERIES_ID)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "series_id": SERIES_ID,
        "series_name": "pixiv 原系列",
        "source": "local",
        "novel_count": 1,
    }


def test_lookup_series_falls_back_to_pixiv_without_writing(
    client, session_factory, pixiv,
):
    """预览是只读的：查到了名字也不落库（落库是 PATCH 的事）。"""
    response = _lookup_series(client, REMOTE_SERIES_ID)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "series_id": REMOTE_SERIES_ID,
        "series_name": "远端系列",
        "source": "pixiv",
        "novel_count": 0,
    }
    assert pixiv.series_calls == [REMOTE_SERIES_ID]
    assert _series(session_factory, REMOTE_SERIES_ID) is None


def test_lookup_series_reports_unknown_instead_of_failing(
    client, session_factory, pixiv,
):
    pixiv.fail = True

    response = _lookup_series(client, REMOTE_SERIES_ID)

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "unknown"
    assert response.json()["series_name"] is None


def test_series_and_date_can_be_cleared_with_null(client, session_factory):
    _seed_local(session_factory, series_name="某系列", series_index=2)

    response = _patch(client, LOCAL_ID, {
        "series_index": None, "create_time": None,
    })

    assert response.status_code == 200, response.text
    row = _row(session_factory, LOCAL_ID)
    assert row.series_index is None
    assert row.create_time is None
    # 未提交的 series_name 不动（它不再是可编辑字段，只能随 series_id 变）
    assert row.series_name == "某系列"


@pytest.mark.parametrize("payload,expected", [
    ({"series_index": 0}, 400),       # 0 = 无章节号 → 显式拒绝（正整数或空）
    ({"series_index": -1}, 400),      # 业务规则 → ValidationError → 400
    ({"series_index": 3.5}, 422),     # Pydantic 整数校验 → 422
])
def test_series_index_must_be_a_positive_int_or_null(
    client, session_factory, payload, expected,
):
    """章节号的口径是**正整数或空**（2026-09-22 统一）。

    ``0`` 与 NULL 同义（"没有章节号"）——来源的 ``guess_series_order`` 在
    ``nextNovel.contentOrder == 1`` 时会算出 0，存进库里就是一个看起来像
    "第 0 本"的假值；命名模板也按同一口径把 0 当空（``core/services.py``）。
    所以写入侧明确拒绝 0，把歧义挡在库外。
    """
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, payload).status_code == expected
    assert _row(session_factory, LOCAL_ID).series_index is None


def test_series_index_zero_is_rejected_with_the_frozen_message(
    client, session_factory,
):
    """0 的 400 文案是契约（前端直接展示 detail）：正好是这一句。"""
    _seed_local(session_factory)

    response = _patch(client, LOCAL_ID, {"series_index": 0})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "章节号必须是正整数"
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
    # 0 与 None 的差别只在"能不能写"：两者都表示无章节号，但 0 提交即拒。
    with pytest.raises(ValidationError, match="章节号必须是正整数"):
        _series_index(0)
    assert _series_index(None) is None
    assert _series_index(1) == 1


def test_author_id_helper_guards_non_int_types():
    """同 ``_series_index``：``_author_id`` 是"绕过 HTTP 直接调用用例"的防线。"""
    from copixiv.core.exceptions import ValidationError
    from copixiv.features.novels.update_metadata import _author_id

    with pytest.raises(ValidationError):
        _author_id(True)
    with pytest.raises(ValidationError):
        _author_id("777")            # 字符串不是身份
    with pytest.raises(ValidationError):
        _author_id(0)
    with pytest.raises(ValidationError):
        _author_id(-1)
    with pytest.raises(ValidationError):
        _author_id(2**64)            # 超出 BigInteger
    assert _author_id(777) == 777


def test_title_length_is_capped(client, session_factory):
    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"title": "x" * 301}).status_code == 400
    assert _patch(client, LOCAL_ID, {"title": "x" * 300}).status_code == 200


# ---------------------------------------------------------------------------
# ④-b caption：仅本地书可编辑（2026-09-22）
# ---------------------------------------------------------------------------

def test_local_caption_is_editable_and_trimmed(client, session_factory):
    """本地书填简介：strip 后落库，响应带新值（前端据此原地换卡片）。"""
    _seed_local(session_factory)

    response = _patch(client, LOCAL_ID, {"caption": "  我的简介\n第二行  "})

    assert response.status_code == 200, response.text
    assert response.json()["caption"] == "我的简介\n第二行"
    assert _row(session_factory, LOCAL_ID).caption == "我的简介\n第二行"


def test_caption_empty_string_and_null_both_clear_it(client, session_factory):
    """清空简介：空串与显式 null 都落成 ``NULL``（写入侧只有一种"没有简介"）。"""
    _seed_local(session_factory, caption="先有一条简介")

    cleared = _patch(client, LOCAL_ID, {"caption": ""})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["caption"] is None
    assert _row(session_factory, LOCAL_ID).caption is None

    # 再写一条，用显式 null 清第二次：两种写法结果一致
    assert _patch(client, LOCAL_ID, {"caption": "又一条"}).status_code == 200
    assert _row(session_factory, LOCAL_ID).caption == "又一条"
    assert _patch(client, LOCAL_ID, {"caption": None}).status_code == 200
    assert _row(session_factory, LOCAL_ID).caption is None


def test_caption_length_is_capped(client, session_factory):
    """2000 字是产品口径（列是 Text）：刚好放行，超一字 400。"""
    from copixiv.features.novels.update_metadata import MAX_CAPTION

    _seed_local(session_factory)

    assert _patch(client, LOCAL_ID, {"caption": "x" * (MAX_CAPTION + 1)}).status_code == 400
    accepted = _patch(client, LOCAL_ID, {"caption": "x" * MAX_CAPTION})
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()["caption"]) == MAX_CAPTION


def test_caption_is_not_touched_when_absent(client, session_factory):
    """PATCH 三态：不提交 caption = 一个字都不动（它是可编辑字段，不是"跟着清"）。"""
    _seed_local(session_factory, caption="原简介")

    assert _patch(client, LOCAL_ID, {"series_index": 2}).status_code == 200
    assert _row(session_factory, LOCAL_ID).caption == "原简介"


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
    """前置工作（b76e8cd）的意义所在：结构性安全的字段改过就顶不掉。

    2026-09-22 更新：作者"改嫁"那条路已经封掉（pixiv 书的 ``author_id`` 只读，
    见 ②）——作者身份错配的出口是**同步**（``test_novel_sync.py``）。所以这里
    只剩"可编辑的三个字段改过就留存" + "对照：来源权威字段照旧被覆盖"。
    """
    _seed_pixiv(session_factory)
    # 作者身份不再能手改：提交即 400，来源值保持不动。
    assert _patch(
        client, PIXIV_ID, {"author_id": REMOTE_AUTHOR_ID},
    ).status_code == 400
    assert _patch(client, PIXIV_ID, {
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
    assert row.author_id == AUTHOR_ID             # 来源值，一个字节没被动过
    assert row.author_name == "pixiv 原作者"        # 名字也不在覆盖白名单里
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
        "title": "改了标题", "author_id": REMOTE_AUTHOR_ID, "tags": ["x"],
    }).status_code == 200

    after = _row(session_factory, LOCAL_ID)
    assert after.source == "local"
    assert after.source_key == before.source_key
    assert after.has_epub == before.has_epub == 3
    assert after.path == before.path            # 改标题不移动文件（§10-6 拍板）
    assert after.id == before.id
    assert after.author_id == REMOTE_AUTHOR_ID  # 唯一的"结构"变化：挂上作者


# ---------------------------------------------------------------------------
# ⑧ 404
# ---------------------------------------------------------------------------

def test_unknown_novel_is_404(client, session_factory):
    response = _patch(client, 123_456_789, {"tags": ["x"]})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
