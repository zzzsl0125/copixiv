"""「我的上传」上传端点测试 —— ``POST /api/library/uploads``。

权威：``docs/LIBRARY_DESIGN.md`` §4（上传管线 / 响应契约）与 §7（不变量）。

用与 ``create_app()`` 同样的接线（router + DomainError handler + app.state 依赖）
搭一个最小 app，``file_storage`` 指向 ``tmp_path``。响应形状按前端
``frontend/src/types/uploads.ts`` 钉死：逐条结果 + 三个计数；部分成功 200；
无文件 400；失败条目不写任何库表。

预设作者（可选表单字段 ``author_id``，整批共用）的契约在这里钉住：本地表命中
零 pixiv 调用（替身的 ``calls``）、整批只解析一次、``0`` / 负数 → 400、
``"abc"`` → 422、重复件响应回显 ``author_name``；以及**解析在写事务之外**的
语句级证明（问到 pixiv 的那一刻，``pg_advisory_xact_lock`` 还没发出去）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError, ValidationError
from copixiv.db.models import Author, FailedNovel, Novel
from copixiv.deps import get_session_factory, get_uow, get_write_uow
from copixiv.features.library import api as library
from copixiv.features.library.ingest import (
    DECODE_ERROR,
    UNKNOWN_AUTHOR,
    body_fingerprint,
)
from copixiv.features.novels import api as novels
from copixiv.features.novels.update_metadata import _author_id
from copixiv.storage.file_storage import FileStorage

# 四种候选编码（utf-16 必须带 BOM）全部失败的字节流。
UNDECODABLE = b"\xff\x00\xff\x00\xff"

# 预设作者用的两个 id：前者本地 ``author`` 行有名字（零网络），后者只能问 pixiv。
AUTHOR_ID = 555_001
REMOTE_AUTHOR_ID = 777_001


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


class _FakeTaskManager:
    def run_task(self, name: str, params: dict | None = None) -> int:
        return 1


class _FakePixivClient:
    """``user_detail`` 替身：作者名解析的 pixiv 兜底在这里被观测。

    ``calls`` 断言"本地表命中就不打网络"与"整批只解析一次"；``sql_log`` /
    ``write_uows`` 由夹具挂上——``user_detail`` 在被调用的一刻把**已经发出去的
    SQL** 与**写 UoW 有没有开过 session** 拷下来，用来证明解析在写事务之外
    （``SqlUnitOfWork.session`` 是懒创建的，见 ``db/uow.py``：没碰过 = 还没开
    事务；写路径的第一条语句则是 ``ingest_uploads`` 的事务级咨询锁）。
    """

    def __init__(self, names: dict[int, str] | None = None):
        self.names = {} if names is None else names
        self.calls: list[int] = []
        self.sql_log: list[str] | None = None
        self.sql_when_called: list[str] = []
        self.write_uows: list = []
        self.write_session_open_at_call: list[bool] = []

    async def user_detail(self, user_id: int) -> dict:
        self.calls.append(user_id)
        if self.sql_log is not None:
            self.sql_when_called = list(self.sql_log)
        self.write_session_open_at_call = [
            uow._session is not None for uow in self.write_uows
        ]
        return {"user": {"id": user_id, "name": self.names.get(user_id, "")}}


@pytest.fixture
def pixiv():
    return _FakePixivClient({REMOTE_AUTHOR_ID: "远端作者"})


@pytest.fixture
def statements(pg_engine, pixiv):
    """引擎级语句记录（用完即摘），并挂给 pixiv 替身（见它的 docstring）。"""
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(pg_engine, "before_cursor_execute", _before)
    pixiv.sql_log = seen
    try:
        yield seen
    finally:
        pixiv.sql_log = None
        event.remove(pg_engine, "before_cursor_execute", _before)


@pytest.fixture
def download_dir(tmp_path) -> Path:
    return tmp_path / "download"


@pytest.fixture
def client(session_factory, download_dir, pixiv):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = AppConfig()
    app.state.file_storage = FileStorage(download_dir=str(download_dir))
    app.state.task_manager = _FakeTaskManager()
    # 端点依赖 get_pixiv_client（预设作者的名字解析要一次 pixiv 兜底），
    # 与 create_app 一样必须挂上——替身让"打没打网络"可断言。
    app.state.client = pixiv

    # 写 UoW 的探针：把 ``get_write_uow`` 包一层、把产物记给替身。包装后行为与
    # 原依赖逐字相同（只是多了一次 append）——它给的证据见
    # ``test_resolution_happens_outside_the_write_transaction``。
    async def _spy_write_uow(session_factory=Depends(get_session_factory)):
        async for uow in get_write_uow(session_factory):
            pixiv.write_uows.append(uow)
            yield uow

    app.dependency_overrides[get_write_uow] = _spy_write_uow

    app.include_router(library.router, prefix="/api/library", tags=["library"])
    # 顺带挂 novels —— 「我的上传」书架就是它的一次特殊搜索（§5）
    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])

    with TestClient(app) as c:
        yield c


def _upload(client, files: list[tuple[str, bytes]], author_id=None):
    """multipart，字段名 ``files`` 重复多次（前端的调用形态）。

    ``author_id`` 是可选表单字段（整批共用）。判据是 ``is None`` 而不是真值：
    "不给这个字段"（旧行为）与"给 0"（400）是两件不同的事。
    """
    form = None if author_id is None else {"author_id": str(author_id)}
    return client.post(
        "/api/library/uploads",
        files=[("files", (name, data, "text/plain")) for name, data in files],
        data=form,
    )


def _local_rows(session_factory) -> list[Novel]:
    with session_factory() as session:
        return list(session.execute(
            select(Novel).where(Novel.source == "local")
        ).scalars())


def _seed_author(session_factory, author_id: int, name: str) -> None:
    """种一行有名字的 ``author``（"本地表命中"的前提）。"""
    with session_factory() as session:
        session.add(Author(author_id=author_id, author_name=name))
        session.commit()


def _author_row(session_factory, author_id: int) -> Author | None:
    with session_factory() as session:
        return session.get(Author, author_id)


# ---------------------------------------------------------------------------
# 响应契约
# ---------------------------------------------------------------------------


def test_created_item_matches_the_wire_contract(client, session_factory):
    response = _upload(client, [("我的书.txt", "第一行\r\n第二行".encode("utf-8"))])

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 1 and body["duplicate"] == 0 and body["failed"] == 0
    assert len(body["results"]) == 1

    item = body["results"][0]
    # 契约字段一字不差；未用到的可选字段不下发（response_model_exclude_none）
    assert set(item) == {
        "filename", "status", "novel_id", "title", "author_name", "text",
    }
    assert item["filename"] == "我的书.txt"
    assert item["status"] == "created"
    assert item["novel_id"] < 0
    assert item["title"] == "我的书"
    assert item["author_name"] == UNKNOWN_AUTHOR
    assert item["text"] == len("第一行\n第二行")

    row = _local_rows(session_factory)[0]
    assert row.id == item["novel_id"]


def test_partial_success_returns_200_with_per_item_results(client, session_factory):
    response = _upload(client, [
        ("我的书.txt", "我的正文".encode("utf-8")),
        ("dup.txt", "我的正文".encode("utf-8")),
        ("bad.txt", UNDECODABLE),
    ])

    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["duplicate"], body["failed"]) == (1, 1, 1)

    created, duplicate, failed = body["results"]
    assert created["status"] == "created" and created["novel_id"] < 0
    # duplicate 指出已在库中那本
    assert duplicate["status"] == "duplicate"
    assert duplicate["novel_id"] == created["novel_id"]
    assert duplicate["title"] == created["title"] == "我的书"
    # failed 只有 filename / status / error
    assert failed == {
        "filename": "bad.txt", "status": "failed", "error": DECODE_ERROR,
    }
    # 失败项不写库（§4-7：连 failed_novel 也不写）
    with session_factory() as session:
        assert session.execute(select(FailedNovel)).first() is None
    assert len(_local_rows(session_factory)) == 1


def test_no_files_is_400(client):
    assert client.post("/api/library/uploads").status_code == 400
    assert client.post("/api/library/uploads", files=[]).status_code == 400
    # 带了别的表单字段但没有 files，同样 400
    assert client.post(
        "/api/library/uploads", data={"note": "x"},
    ).status_code == 400


# ---------------------------------------------------------------------------
# 落库形态（§3/§7-3/§7-4/§7-5）
# ---------------------------------------------------------------------------


def test_row_values_and_file_location(client, session_factory, download_dir):
    _upload(client, [("【某某】我的书(完结).txt", "正文内容".encode("utf-8"))])

    row = _local_rows(session_factory)[0]
    assert row.source == "local"
    assert row.source_key == body_fingerprint("正文内容")
    assert row.has_epub == 3                      # §7-4 / §3.3
    assert row.author_name == "某某"              # §7-3（非 NULL）
    assert row.author_id is None and row.series_id is None
    assert row.like == 0 and row.view == 0
    assert row.text == len("正文内容")
    assert row.create_time is not None
    # §7-5：path 落在 download/local/ 下，且文件真在磁盘上
    path = Path(row.path)
    assert path.parent == download_dir / "local"
    assert path.name.startswith("我的书_")
    assert path.read_text(encoding="utf-8") == "正文内容"


def test_undecodable_upload_writes_nothing(client, session_factory):
    response = _upload(client, [("坏.txt", UNDECODABLE)])

    assert response.json()["results"][0]["error"] == DECODE_ERROR
    with session_factory() as session:
        assert session.execute(select(Novel)).first() is None
        assert session.execute(select(FailedNovel)).first() is None


# ---------------------------------------------------------------------------
# §7-7：二次上传被拒，且唯一索引在数据库层生效
# ---------------------------------------------------------------------------


def test_second_upload_is_duplicate_and_unique_index_holds(
    client, session_factory,
):
    _upload(client, [("第一次.txt", "同一份正文".encode("utf-8"))])
    row = _local_rows(session_factory)[0]

    response = _upload(client, [("第二次.txt", "同一份正文".encode("utf-8"))])
    item = response.json()["results"][0]
    assert item["status"] == "duplicate"
    assert item["novel_id"] == row.id
    assert item["title"] == row.title
    assert len(_local_rows(session_factory)) == 1

    # 数据库层：同 (source, source_key) 再来一行直接被 ux_novel_source_key 拒
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO novel (id, title, author_name, path, \"like\", "
                    "view, \"text\", has_epub, shuffle, tags, is_favourite, "
                    "source, source_key) VALUES (:id, '手工复制', '未知作者', "
                    "'download/local/manual.txt', 0, 0, 0, 3, 0, '{}', false, "
                    "'local', :key)"
                ),
                {"id": -999_999_000, "key": row.source_key},
            )
            session.commit()

    # pixiv 行的 source_key 为 NULL —— 部分唯一索引不影响存量
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO novel (id, title, author_name, path, \"like\", "
                "view, \"text\", has_epub, shuffle, tags, is_favourite) "
                "VALUES (42, 'pixiv 书', '作者', 'download/0000/x.txt', 0, 0, "
                "0, 0, 0, '{}', false)"
            )
        )
        session.commit()
        assert session.execute(
            select(Novel.source).where(Novel.id == 42)
        ).scalar() == "pixiv"


# ---------------------------------------------------------------------------
# 列表 / 详情响应带上 source（前端卡片据此显示「本地」徽章，§2/§5）
# ---------------------------------------------------------------------------


def test_list_and_detail_carry_source(client, session_factory):
    upload = _upload(client, [("本地书.txt", "本地正文".encode("utf-8"))])
    local_id = upload.json()["results"][0]["novel_id"]
    with session_factory() as session:
        session.add(Author(author_id=42, author_name="作者42"))
        session.flush()
        session.add(Novel(
            id=42, title="pixiv 书", author_id=42, author_name="作者42",
            path="download/0000/x.txt", like=5000, text=8000,
        ))
        session.commit()

    # 「我的上传」书架 = keyword=source:local; + 阈值归零（§5）
    response = client.get("/api/novels/", params={
        "keyword": "source:local;", "min_like": 0, "min_text": 0,
        "exclude_blocked": "false",
    })
    assert response.status_code == 200
    listed = response.json()["novels"]
    assert [n["id"] for n in listed] == [local_id]
    assert listed[0]["source"] == "local"

    # 阈值不归零就是空书架（§5 唯一不能省的一步）—— 服务端不做默认阈值，
    # 前端会把 config.ts 的 500 / 3000 显式带上（App.vue）。
    assert client.get("/api/novels/", params={
        "keyword": "source:local;", "min_like": 500, "min_text": 3000,
        "exclude_blocked": "false",
    }).json()["novels"] == []

    # 详情（by-ids，卡片展开用）同样带 source；pixiv 行是默认值
    detail = client.post("/api/novels/by-ids", json={
        "novel_ids": [local_id, 42],
    }).json()["novels"]
    assert {n["id"]: n["source"] for n in detail} == {
        local_id: "local", 42: "pixiv",
    }


# ---------------------------------------------------------------------------
# 预设作者：可选表单字段 ``author_id``（整批共用）
# ---------------------------------------------------------------------------


def test_author_id_uses_the_local_name_without_asking_pixiv(
    client, session_factory, pixiv,
):
    """happy path：本地 ``author`` 行有名字 → 整批零网络，文件名作者被覆盖。"""
    _seed_author(session_factory, AUTHOR_ID, "本地作者")

    response = _upload(
        client, [("【文件名作者】我的书.txt", "正文".encode("utf-8"))],
        author_id=AUTHOR_ID,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["duplicate"], body["failed"]) == (1, 0, 0)
    item = body["results"][0]
    assert item["author_name"] == "本地作者"

    row = _local_rows(session_factory)[0]
    assert (row.author_id, row.author_name) == (AUTHOR_ID, "本地作者")
    assert pixiv.calls == []          # 本地表命中 → 一次 pixiv 都不问


def test_author_id_falls_back_to_pixiv_once_per_batch(
    client, session_factory, pixiv,
):
    """本地表没有 → 问一次 pixiv；**整批只解析一次**（一个 id 写 N 本）。"""
    response = _upload(
        client,
        [("甲.txt", "甲正文".encode("utf-8")), ("乙.txt", "乙正文".encode("utf-8"))],
        author_id=REMOTE_AUTHOR_ID,
    )

    assert response.status_code == 200
    items = response.json()["results"]
    assert [item["author_name"] for item in items] == ["远端作者", "远端作者"]
    assert pixiv.calls == [REMOTE_AUTHOR_ID]

    rows = _local_rows(session_factory)
    assert {r.author_id for r in rows} == {REMOTE_AUTHOR_ID}
    assert {r.author_name for r in rows} == {"远端作者"}
    # 作者行也收尾写上名字（占位行由读事务的 ensure_exists 建）
    assert _author_row(session_factory, REMOTE_AUTHOR_ID).author_name == "远端作者"


def test_resolution_happens_outside_the_write_transaction(
    client, pixiv, statements,
):
    """**解析在写事务之外**的语句/会话级证明（模块 docstring 第 1 条）。

    三条互补的证据，全部取"问到 pixiv 的那一刻"的快照：

    1. **写 UoW 连 session 都还没开**（``SqlUnitOfWork.session`` 懒创建：
       没碰过 = 没开事务；psycopg2 的服务器端事务要到第一条语句才真正开始）。
       把解析搬进 ``async with write_uow.begin():`` 里——哪怕只是"先 select 本地
       表"——第一步就会在这里露出来；
    2. 窗口里还没有 ``pg_advisory_xact_lock``：它是 ``ingest_uploads`` 的**第一条**
       语句（``_lock_local_ids``）；
    3. 窗口里没有任何 INSERT/UPDATE/DELETE —— "先建 author 占位行、再问 pixiv"
       这种写法也会在这里红。

    反面断言（整段窗口里锁与写语句确实出现过）用来防止这个证明退化成空断言：
    网络确实发生过、写路径也确实跑过。
    """
    response = _upload(
        client, [("书.txt", "正文".encode("utf-8"))], author_id=REMOTE_AUTHOR_ID,
    )
    assert response.status_code == 200
    assert pixiv.calls == [REMOTE_AUTHOR_ID]

    at_call = pixiv.sql_when_called
    assert at_call, "替身没拿到语句记录 —— 这个证明会退化成空断言"
    assert pixiv.write_session_open_at_call == [False]
    assert [s for s in at_call if "pg_advisory_xact_lock" in s] == []
    assert [
        s for s in at_call
        if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ] == []
    # 反面：整段窗口里锁与写语句确实出现过（写路径真的跑了）
    assert [s for s in statements if "pg_advisory_xact_lock" in s]
    assert [s for s in statements if s.lstrip().upper().startswith("INSERT")]


def test_endpoint_declares_the_read_uow_before_the_write_uow():
    """依赖形态：读事务（解析）先声明，写事务（落库 + 咨询锁）后声明。

    语句级证明在上面那条测试里；这里补一条静态断言——两个依赖的名字一旦被
    调换/删掉（比如有人"顺手"把解析搬进写事务），这条会先红。
    """
    parameters = inspect.signature(library.upload_novels).parameters
    assert parameters["uow"].default.dependency is get_uow
    assert parameters["write_uow"].default.dependency is get_write_uow
    assert list(parameters).index("uow") < list(parameters).index("write_uow")


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_author_id_is_400(client, session_factory, bad):
    """``0`` / 负数 → 400，文案来自 ``update_metadata._author_id``（唯一所有者）。"""
    # 期望文案从唯一所有者那里取，不抄第二份（两处一旦漂移这里就红）
    with pytest.raises(ValidationError) as excinfo:
        _author_id(bad)

    response = _upload(client, [("书.txt", "正文".encode("utf-8"))], author_id=bad)

    assert response.status_code == 400
    assert response.json()["detail"] == excinfo.value.detail
    # 校验在落库之前：这一批一个字节都没写（novel / author 都没有）
    with session_factory() as session:
        assert session.execute(select(Novel)).first() is None
        assert session.execute(select(Author)).first() is None


def test_non_integer_author_id_is_422(client, session_factory):
    """``"abc"`` 是类型错误（FastAPI 类型转换阶段）→ 422，不是业务 400。"""
    response = _upload(
        client, [("书.txt", "正文".encode("utf-8"))], author_id="abc",
    )

    assert response.status_code == 422
    with session_factory() as session:
        assert session.execute(select(Novel)).first() is None


def test_duplicate_response_echoes_the_preset_author(client, session_factory, pixiv):
    """重复件也应用预设：状态/novel_id/title 不变，另回显这次写进去的作者名。"""
    _seed_author(session_factory, AUTHOR_ID, "本地作者")
    first = _upload(
        client, [("【旧作者】第一次.txt", "同一份正文".encode("utf-8"))],
    ).json()["results"][0]

    response = _upload(
        client, [("第二次.txt", "同一份正文".encode("utf-8"))], author_id=AUTHOR_ID,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["duplicate"], body["failed"]) == (0, 1, 0)
    item = body["results"][0]
    assert item["status"] == "duplicate"
    assert item["novel_id"] == first["novel_id"]       # 既有语义
    assert item["title"] == first["title"]
    assert item["author_name"] == "本地作者"            # 回显（前端显示「已挂作者：X」）
    assert "text" not in item and "error" not in item

    row = _local_rows(session_factory)[0]
    assert (row.author_id, row.author_name) == (AUTHOR_ID, "本地作者")
    assert len(_local_rows(session_factory)) == 1      # 没建新行
    assert pixiv.calls == []                           # 本地表命中 → 零网络


def test_duplicate_without_a_preset_keeps_the_old_response_shape(
    client, session_factory,
):
    """没带 ``author_id`` → 响应与旧行为逐字一致（``duplicate`` 不带 ``author_name``）。"""
    first = _upload(client, [("第一次.txt", "同一份正文".encode("utf-8"))])
    assert first.status_code == 200
    created = first.json()["results"][0]

    item = _upload(
        client, [("第二次.txt", "同一份正文".encode("utf-8"))],
    ).json()["results"][0]

    assert set(item) == {"filename", "status", "novel_id", "title"}
    assert item["novel_id"] == created["novel_id"]
    row = _local_rows(session_factory)[0]
    assert (row.author_id, row.author_name) == (None, created["author_name"])


def test_a_fully_failed_batch_writes_no_novel_and_no_author_name(
    client, session_factory, pixiv,
):
    """整批失败（解码不出）→ 不写 novel / failed_novel，也不给作者行写名字。

    读事务里为外键安全建的占位行会留下（``author_name`` 仍是 NULL）——这里把它
    一起断言下来，免得"失败不写表"被读成"连占位行都没有"。
    """
    response = _upload(client, [("坏.txt", UNDECODABLE)], author_id=REMOTE_AUTHOR_ID)

    assert response.status_code == 200
    assert response.json()["failed"] == 1
    with session_factory() as session:
        assert session.execute(select(Novel)).first() is None
        assert session.execute(select(FailedNovel)).first() is None
    author = _author_row(session_factory, REMOTE_AUTHOR_ID)
    assert author is not None and author.author_name is None
    # 解析照旧做了一次，而且在写事务之外（见上面那条语句级证明）
    assert pixiv.calls == [REMOTE_AUTHOR_ID]
