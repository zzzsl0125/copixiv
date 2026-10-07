"""单本同步端点测试 —— ``POST /api/novels/{novel_id}/sync``。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §4.3 的单本版（P1「重新抓取，恢复来源值」）
+ ``features/novels/field_policy.py``（pixiv 书的 ``title`` / ``author_id`` /
``series_id`` 只读，同步是它们的修正入口）。本文件钉住四组事实：

* **成功**：``title`` / ``author_id`` / ``series_id`` / ``create_time`` 被拉回
  来源值，响应与 PATCH **同形状**（同一个 ``NovelBase``，含 ``editable_fields``）；
* **拒**：本地书 → 400（没有来源可恢复，且一个网络调用都不发）；pixiv 上取不到
  （webview 返回空 = 已删除 / 受限，或客户端抛 ``NovelNotFoundError``）→ 404；
  库里没有这一行 → 404；
* **只动该动的列**：``has_epub`` 一个字节不改（本地产物的状态机归产线所有，
  2026-08「22.6 万行 has_epub 失明」是同类风险）、``path`` 不改道（文件锚，标题
  变了也不改名）、``series_index`` 不被 webview 的**猜测**抹掉；
* **tags 只增不减**（``_upsert_rows`` 的并集语义）：用户自己加的标签同步不会丢。
  另外 ``text`` 按来源字数更新（来源权威）。

再加一条仓库纪律的执法：**抓取发生在写事务之外**——用"另一条连接抢同一行的写
锁"证明（若 webview 调用时挂着写事务，那一行正被锁着，探测会超时）。

夹具风格照抄 ``tests/api/test_novel_update.py``：最小 app + DomainError 处理器
+ app.state 依赖；清库由 ``clean_db`` 负责（PG 共享测试库）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError, NovelNotFoundError
from copixiv.db.models import Author, Novel, Series
from copixiv.features.novels import api as novels
from copixiv.storage.file_storage import FileStorage

LOCAL_ID = -999_000_001              # 本地书：负数锚点段（LIBRARY_DESIGN.md §3.1）
PIXIV_ID = 987_654
OLD_AUTHOR_ID = 555                  # 库里这一行原本挂的作者
OLD_SERIES_ID = 606                  # 库里这一行原本挂的系列
NEW_AUTHOR_ID = 777_001              # 来源要拉回的作者（本地表没有）
NEW_SERIES_ID = 888_888              # 来源要拉回的系列（本地表没有）
STORED_PATH = f"download/0098/库里的标题_{PIXIV_ID}.txt"
OLD_TIME = datetime(2019, 1, 1, 8, 0, tzinfo=timezone.utc)
# "2020-05-05 12:00:00" 是裸时间串 → 按 Asia/Tokyo 解析（parse_pixiv_time 的既有约定）
SOURCE_TIME = datetime(2020, 5, 5, 3, 0, tzinfo=timezone.utc)
# 正文里带图占位符：draft 会据此判 PENDING(1)——has_epub 若被写入就会露馅。
SOURCE_BODY = "来源正文 [uploadedimage:12345] 尾巴"


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


def _webview_payload(**over) -> SimpleNamespace:
    """``webview_novel`` 的响应形状（字段对齐 ``build_from_webview`` 的读法）。"""
    data = dict(
        id=PIXIV_ID,
        title="来源新标题",
        text=SOURCE_BODY,
        user_id=NEW_AUTHOR_ID,
        rating=SimpleNamespace(bookmark=999, view=888),
        caption="来源新简介",
        series_id=NEW_SERIES_ID,
        series_title="来源系列名",
        series_navigation=None,
        cdate="2020-05-05 12:00:00",
        tags=["来源新标签"],
        images=None,
        illusts=None,
        cover_url=None,
    )
    data.update(over)
    return SimpleNamespace(**data)


class _FakePixivClient:
    """``webview_novel``（同步的取数口）+ ``user_detail``（作者名兜底）。

    ``probe`` 是抓取那一刻执行的回调（用来证明当时没有写事务），``not_found``
    模拟 pixiv 侧的确定性不存在（客户端抛 ``NovelNotFoundError``），
    ``fail_user_detail`` 模拟作者名解析不出来（名字是缓存，不是失败）。
    """

    def __init__(self, payload=None):
        self.payload = payload
        self.probe = None
        self.not_found = False
        self.fail_user_detail = False
        self.webview_calls: list[int] = []
        self.user_detail_calls: list[int] = []

    async def webview_novel(self, novel_id: int):
        self.webview_calls.append(novel_id)
        if self.probe is not None:
            self.probe()
        if self.not_found:
            raise NovelNotFoundError(f"novel {novel_id} not found")
        return self.payload

    async def user_detail(self, user_id: int) -> dict:
        self.user_detail_calls.append(user_id)
        if self.fail_user_detail:
            raise RuntimeError("pixiv unavailable")
        return {"user": {"id": user_id, "name": "来源作者名"}}


@pytest.fixture
def pixiv():
    return _FakePixivClient(_webview_payload())


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
    app.state.client = pixiv

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 造数 / 读取
# ---------------------------------------------------------------------------

def _seed_pixiv(session_factory, **over) -> Novel:
    """一行"来源曾经抓过、之后作者/系列被改错"的 pixiv 书。

    四个来源值刻意都与 webview 响应不同（标题 / 作者 / 系列 / 日期），
    以及 ``has_epub=3``（draft 会判 1）、``series_index=7``（webview 猜不出来）、
    一条用户自己加的标签——每一个都是"同步只动该动的列"的观测点。
    """
    values = dict(
        id=PIXIV_ID,
        title="库里的标题",
        author_id=OLD_AUTHOR_ID,
        author_name="旧作者名",
        path=STORED_PATH,
        like=10, view=20, text=2000,
        caption="库里旧简介",
        series_id=OLD_SERIES_ID,
        series_name="库里旧系列",
        series_index=7,
        create_time=OLD_TIME,
        has_epub=3,
        tags=["我加的标签", "来源老标签"],
        source="pixiv",
        source_key=None,
    )
    values.update(over)
    with session_factory() as s:
        s.add(Author(author_id=OLD_AUTHOR_ID, author_name="旧作者名"))
        # series_id 有外键：造一行带 id 的小说就必须造系列行（名字由推导而来，
        # 这里故意给个旧名字，观测点之一）。
        s.add(Series(series_id=OLD_SERIES_ID, series_name="库里旧系列"))
        s.add(Novel(**values))
        s.commit()
    return values


def _seed_local(session_factory, **over):
    values = dict(
        id=LOCAL_ID,
        title="本地书",
        author_id=None,
        author_name="未知作者",
        path=f"download/local/本地书_{LOCAL_ID}.txt",
        like=0, view=0, text=10,
        has_epub=3,
        tags=[],
        source="local",
        source_key="a" * 64,
    )
    values.update(over)
    with session_factory() as s:
        s.add(Novel(**values))
        s.commit()


def _row(session_factory, nid: int) -> Novel:
    with session_factory() as s:
        return s.get(Novel, nid)


def _author(session_factory, author_id: int):
    with session_factory() as s:
        return s.get(Author, author_id)


def _sync(client, nid: int = PIXIV_ID):
    return client.post(f"/api/novels/{nid}/sync")


# ---------------------------------------------------------------------------
# ① 成功：来源值被拉回来
# ---------------------------------------------------------------------------

def test_sync_restores_the_source_values(client, session_factory, pixiv):
    _seed_pixiv(session_factory)

    response = _sync(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == PIXIV_ID
    assert body["title"] == "来源新标题"
    assert body["author_id"] == NEW_AUTHOR_ID
    assert body["author_name"] == "来源作者名"       # webview 没名字 → 表里/远端的缓存
    assert body["series_id"] == NEW_SERIES_ID
    assert body["series_name"] == "来源系列名"
    assert datetime.fromisoformat(body["create_time"]) == SOURCE_TIME
    assert body["caption"] == "来源新简介"
    assert (body["like"], body["view"]) == (999, 888)
    assert body["text"] == len(SOURCE_BODY)          # 来源权威：按来源字数更新
    assert body["source"] == "pixiv"
    assert body["editable_fields"] == ["series_index", "tags", "create_time"]
    assert pixiv.webview_calls == [PIXIV_ID]

    # 库里也真的落了同样的值（响应不是内存里的临时对象）
    row = _row(session_factory, PIXIV_ID)
    assert row.title == "来源新标题"
    assert row.author_id == NEW_AUTHOR_ID and row.author_name == "来源作者名"
    assert row.series_id == NEW_SERIES_ID and row.series_name == "来源系列名"
    assert row.create_time == SOURCE_TIME
    assert row.text == len(SOURCE_BODY)
    # 作者名顺手写回 author 行（名字是作者的属性，不是这一行的私产）
    assert _author(session_factory, NEW_AUTHOR_ID).author_name == "来源作者名"


def test_sync_response_has_the_same_shape_as_patch(client, session_factory):
    """与 PATCH 同形状：前端两处用同一套"原地替换卡片"的代码。"""
    _seed_pixiv(session_factory)

    patched = client.patch(f"/api/novels/{PIXIV_ID}", json={"tags": ["x"]})
    synced = _sync(client)

    assert patched.status_code == 200, patched.text
    assert synced.status_code == 200, synced.text
    assert set(synced.json()) == set(patched.json())


def test_sync_reads_the_author_name_from_the_local_table_first(
    client, session_factory, pixiv,
):
    """本地 author 表已有这个名字 → 不打网络（与编辑器同一条链）。"""
    _seed_pixiv(session_factory)
    with session_factory() as s:
        s.add(Author(author_id=NEW_AUTHOR_ID, author_name="库里已知的作者"))
        s.commit()

    assert _sync(client).status_code == 200

    assert _row(session_factory, PIXIV_ID).author_name == "库里已知的作者"
    assert pixiv.user_detail_calls == []


def test_sync_leaves_the_name_pending_when_nothing_can_resolve_it(
    client, session_factory, pixiv,
):
    """名字是缓存、id 是身份：解析不出来 → NULL（待解析），``sync_empty_name`` 会补。

    这条与编辑器 PATCH 的 ``_author_columns`` 同构——不能因为拿不到名字就不写 id。
    """
    _seed_pixiv(session_factory)
    pixiv.fail_user_detail = True

    response = _sync(client)

    assert response.status_code == 200, response.text
    row = _row(session_factory, PIXIV_ID)
    assert row.author_id == NEW_AUTHOR_ID
    assert row.author_name is None
    # 占位 author 行仍要建（外键 + 追作者页），只是名字待解析
    assert _author(session_factory, NEW_AUTHOR_ID) is not None


# ---------------------------------------------------------------------------
# ② 只动该动的列：has_epub / path / series_index / tags
# ---------------------------------------------------------------------------

def test_sync_never_touches_has_epub_or_path(client, session_factory, pixiv):
    """``has_epub`` 归产线所有、``path`` 是文件锚——同步一个字都不许改。

    draft 这次真的判出了 ``PENDING(1)``（正文带图占位符），标题也变了（算出来的
    路径必然不同）——所以这两条断言都不是空转：值若被写进去，测试会看见。
    """
    from copixiv.core.draft import build_from_webview
    from copixiv.core.models import EpubStatus

    _seed_pixiv(session_factory)
    # 先证明"若写就会变"：draft 对同一份响应的判定就是 PENDING(1)。
    assert build_from_webview(pixiv.payload).has_epub == EpubStatus.PENDING

    assert _sync(client).status_code == 200

    row = _row(session_factory, PIXIV_ID)
    assert row.has_epub == 3                       # draft 判的是 1，库里是 3
    assert row.path == STORED_PATH                 # 标题变了，路径也不跟着改道
    assert row.source == "pixiv"


def test_sync_fills_path_only_when_the_stored_one_is_null(
    client, session_factory, pixiv,
):
    """``path`` 的另一半："非空者胜"只在库中为 NULL 时才被填。

    历史行 / 迁移遗留的行没有文件锚，同步顺手把它补上——这是 ``path`` 唯一允许
    被写入的情形（不是"同步改了它"）。
    """
    _seed_pixiv(session_factory, path=None)

    assert _sync(client).status_code == 200

    row = _row(session_factory, PIXIV_ID)
    assert row.path is not None
    assert row.path.endswith(f"_{PIXIV_ID}.txt")
    assert "来源新标题" in row.path


def test_sync_keeps_the_stored_series_index(client, session_factory, pixiv):
    """webview 的序号是 ``guess_series_order`` 的**猜测**（猜不出 = None）——
    进覆盖白名单就等于用 None 抹掉库里已有的值。"""
    _seed_pixiv(session_factory)

    assert _sync(client).status_code == 200

    assert _row(session_factory, PIXIV_ID).series_index == 7


def test_sync_unions_tags_and_never_drops_the_users_own(
    client, session_factory, pixiv,
):
    """标签只增不减：来源送来的新标签并进去，用户自己加的（以及来源这次没带的）
    一律保留——"同步"不该顺手删掉用户在编辑器里攒下的东西。"""
    _seed_pixiv(session_factory)          # tags = ["我加的标签", "来源老标签"]

    assert _sync(client).status_code == 200

    row = _row(session_factory, PIXIV_ID)
    assert set(row.tags) == {"我加的标签", "来源老标签", "来源新标签"}


# ---------------------------------------------------------------------------
# ③ 拒绝路径：本地书 400、取不到 404
# ---------------------------------------------------------------------------

def test_local_book_has_no_source_to_sync(client, session_factory, pixiv):
    _seed_local(session_factory)

    response = _sync(client, LOCAL_ID)

    assert response.status_code == 400, response.text
    assert "本地书" in response.json()["detail"]
    # 硬拒且不发网络：本地书连抓都不该抓
    assert pixiv.webview_calls == []


def test_unknown_novel_is_404(client, session_factory):
    response = _sync(client, 123_456_789)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_gone_or_restricted_novel_is_404(client, session_factory, pixiv):
    """webview 返回空 = 已删除 / 受限（与下载路径同一个判定），同步回 404。"""
    _seed_pixiv(session_factory)
    pixiv.payload = None

    response = _sync(client)

    assert response.status_code == 404, response.text
    assert "pixiv" in response.json()["detail"]
    # 取不到就不该动库：标题还是库里那个
    assert _row(session_factory, PIXIV_ID).title == "库里的标题"


def test_client_reported_not_found_is_404_too(client, session_factory, pixiv):
    """客户端抛 ``NovelNotFoundError``（确定性 404 / 内容被删）→ 同样的 404。"""
    _seed_pixiv(session_factory)
    pixiv.not_found = True

    assert _sync(client).status_code == 404
    assert _row(session_factory, PIXIV_ID).title == "库里的标题"


# ---------------------------------------------------------------------------
# ④ 接线纪律：抓取在写事务之外 + 端点依赖用 get_uow
# ---------------------------------------------------------------------------

def test_fetch_runs_outside_the_write_transaction(
    client, session_factory, pixiv,
):
    """抓取那一刻，这一行不许被本进程的写事务锁着。

    探测方式：从**另一条连接**对同一行做一次 UPDATE（带 ``lock_timeout``）。
    若 ``webview_novel`` 是在写事务里调用的，那一行正被锁着 → 探测超时 → 报错；
    现在抓取在 ``run_write_transaction`` 之前，探测秒过。
    """
    _seed_pixiv(session_factory)
    ran: list[int] = []

    def probe():
        ran.append(1)
        with session_factory() as s:
            s.execute(sa_text("SET LOCAL lock_timeout = '1500ms'"))
            s.execute(
                sa_text("UPDATE novel SET title = title WHERE id = :i"),
                {"i": PIXIV_ID},
            )
            s.commit()

    pixiv.probe = probe

    assert _sync(client).status_code == 200
    assert ran == [1]           # 探测确实跑过——否则这条测试就是空转


def test_sync_endpoint_uses_the_read_uow_dependency():
    """契约（2026-09-22）：``Depends(get_uow)`` 而不是 ``get_write_uow``。

    写事务由用例里的 ``run_write_transaction`` 自己开——依赖注入阶段开不出
    "先抓取、稍后再写"的事务边界。行为上看不出区别，所以用签名钉住接线。
    """
    import inspect

    from copixiv.deps import get_uow, get_write_uow
    from copixiv.features.novels.api import sync_novel

    dependency = inspect.signature(sync_novel).parameters["uow"].default.dependency
    assert dependency is get_uow
    assert dependency is not get_write_uow
