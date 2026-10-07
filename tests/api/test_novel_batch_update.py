"""批量元数据编辑端点测试 —— ``POST /api/novels/batch-metadata``。

契约：``features/novels/batch_edit_metadata.py``（用例，模块 docstring 里写清了
每条语义的来源与理由）与 ``schemas.BatchMetadataRequest/Response``。本文件钉住
十件事，每件都能红：

1. **预演不写库**（``apply=false``）：返回 ``matched/applicable/skipped``，
   ``affected=0``，且不建占位 author / series 行、不打网络；
2. **混合选择集**：pixiv 书跳过并计数（``applicable``/``skipped``），它们的
   作者 / 系列列**一个字节都没变**；
3. **挂作者**：本地表命中不打 pixiv（解析只做一次，N 本共用），名字写进
   ``author`` 行与 novel 行（含同作者的其它行）；
4. **挂系列**：写 ``series_id`` + ``series_name``，且**只**命名 series 行
   ——同系列其它 novel 行的名字不被改写；
5. **解除作者关联**：显示名保留；``author_name IS NULL`` 的行补「未知作者」；
6. **解除系列关联**：``series_id``/``series_name``/``series_index`` 三列一起清；
7. **解析失败不致命**：pixiv 不可达 → ``author_id`` 照写、名字 NULL、200；
8. **校验**：``0``/负数 400、空 patch 400、空 scope 400、未知字段 / ``true`` 422；
9. **幂等**：重复提交同一请求，库里值不变（逐列 ``IS DISTINCT FROM`` → 第二次
   ``affected=0``）；
10. 写端点形态（``tests/regression/test_m6_write_lock.py`` 的名单登记）。

夹具与 ``tests/api/test_batch_operations.py`` 的 ``client`` / ``_seed`` 同构，
外加 ``tests/api/test_novel_update.py`` 的假 pixiv client（``calls`` 断言"本地命中
不打网络"、``fail`` 模拟不可达）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError
from copixiv.db.models import Author, Novel, Series
from copixiv.features.novels import api as novels
from copixiv.features.novels.update_metadata import UNKNOWN_AUTHOR

# 本地书：负数 id 段（LIBRARY_DESIGN.md §3.1 —— 符号管身份、模管时间）
LOCAL_IDS = [-999_000_001, -999_000_002, -999_000_003]
PIXIV_IDS = [987_654, 987_655]
AUTHOR_ID = 555                  # 本地 author 行有名字的作者（不打网络）
REMOTE_AUTHOR_ID = 777_777       # 本地表没有、只能问 Pixiv 的作者
SERIES_ID = 606                  # 本地 series 行有名字的系列
REMOTE_SERIES_ID = 888_888       # 本地表没有、只能问 Pixiv 的系列


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


class _FakePixivClient:
    """``user_detail`` / ``novel_series`` 替身（与 test_novel_update.py 同形）。

    ``calls`` / ``series_calls`` 让测试能断言"本地命中 / 解析只做一次"，
    ``fail`` 模拟 pixiv 不可达（冷却 / 限流 / 网络故障）——那条路径必须是
    "能存，只是名字待解析"。
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
        novels_payload = (
            [{"id": 1, "series": {"id": series_id, "title": title}}] if title else []
        )
        return {"novels": novels_payload, "next_url": None}


@pytest.fixture
def pixiv():
    return _FakePixivClient()


@pytest.fixture
def client(session_factory, pixiv):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = AppConfig()
    app.state.client = pixiv

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 造数 / 读取 / 请求
# ---------------------------------------------------------------------------

def _seed(
    session_factory,
    novel_id: int,
    *,
    source: str = "local",
    author_id: int | None = None,
    author_name: str | None = None,
    series_id: int | None = None,
    series_name: str | None = None,
    series_index: int | None = None,
    title: str | None = None,
    **over,
):
    """一行书 + 它需要的外键行（author / series 占位行先建，与抓取路径同序）。"""
    values = dict(
        id=novel_id,
        title=title or f"书{novel_id}",
        author_id=author_id,
        author_name=author_name,
        path=f"download/test/{novel_id}.txt",
        like=0, view=0, text=100,
        has_epub=3,
        tags=[],
        source=source,
        source_key=(f"{abs(novel_id):064d}" if source == "local" else None),
        series_id=series_id,
        series_name=series_name,
        series_index=series_index,
    )
    values.update(over)
    with session_factory() as s:
        if author_id is not None and s.get(Author, author_id) is None:
            s.add(Author(author_id=author_id, author_name=author_name))
            s.flush()
        if series_id is not None and s.get(Series, series_id) is None:
            s.add(Series(series_id=series_id, series_name=series_name))
        s.add(Novel(**values))
        s.commit()


def _seed_author(session_factory, author_id: int, name: str | None) -> None:
    with session_factory() as s:
        s.add(Author(author_id=author_id, author_name=name))
        s.commit()


def _seed_series(session_factory, series_id: int, name: str | None) -> None:
    with session_factory() as s:
        s.add(Series(series_id=series_id, series_name=name))
        s.commit()


def _row(session_factory, novel_id: int) -> Novel:
    with session_factory() as s:
        return s.get(Novel, novel_id)


def _author(session_factory, author_id: int):
    with session_factory() as s:
        return s.get(Author, author_id)


def _series(session_factory, series_id: int):
    with session_factory() as s:
        return s.get(Series, series_id)


def _snapshot(session_factory, novel_id: int) -> tuple:
    """一行上"批量编辑可能碰到"的全部列 —— "一个字节都没变"用它比较。"""
    row = _row(session_factory, novel_id)
    return (
        row.author_id, row.author_name,
        row.series_id, row.series_name, row.series_index,
        row.title, row.source, tuple(row.tags),
    )


def _ids_scope(*novel_ids: int) -> dict:
    return {"mode": "ids", "novel_ids": list(novel_ids)}


def _batch(client, payload: dict):
    return client.post("/api/novels/batch-metadata", json=payload)


# ---------------------------------------------------------------------------
# ① 预演：只算数不写库
# ---------------------------------------------------------------------------

class TestPreview:
    def test_preview_counts_without_writing_anything(
        self, client, session_factory, pixiv,
    ):
        """``apply=false``：报数、不写库、不建占位行、不问 Pixiv。

        "先报数再确认"是本功能与单本 PATCH 的唯一交互差异，报数必须**零副作用**
        ——否则用户取消之后库里已经多了作者行。
        """
        _seed(session_factory, LOCAL_IDS[0], author_name="文件名解析出的作者")
        _seed(
            session_factory, PIXIV_IDS[0], source="pixiv",
            author_id=AUTHOR_ID, author_name="pixiv 作者",
        )

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0], PIXIV_IDS[0]),
            "author_id": REMOTE_AUTHOR_ID,
            "apply": False,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 2, "applicable": 1, "skipped": 1, "affected": 0,
        }
        # 本地书一个字没动、作者名没被解析、占位行没建（外键行是写库的前置）
        row = _row(session_factory, LOCAL_IDS[0])
        assert (row.author_id, row.author_name) == (None, "文件名解析出的作者")
        assert _author(session_factory, REMOTE_AUTHOR_ID) is None
        assert pixiv.calls == []

    def test_preview_is_the_default(self, client, session_factory):
        """``apply`` 缺省即预演：漏传这个键不会意外写库。"""
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]), "author_id": AUTHOR_ID,
        })

        assert response.status_code == 200, response.text
        assert response.json()["affected"] == 0
        assert _row(session_factory, LOCAL_IDS[0]).author_id is None
        assert _author(session_factory, AUTHOR_ID) is None, "预演不建占位行"


# ---------------------------------------------------------------------------
# ② 混合选择集：pixiv 书跳过而不是 400
# ---------------------------------------------------------------------------

class TestMixedSelection:
    def test_pixiv_books_are_skipped_with_their_columns_untouched(
        self, client, session_factory,
    ):
        """3 本本地书 + 2 本 pixiv 书：``applicable=3`` / ``skipped=2``。

        单本 PATCH 对 pixiv 书提交这两个字段是硬拒 400（``field_policy``）；
        批量里换成"报数 + 跳过"——选择集天然混合，整体 400 会让功能不可用，
        而跳过的数量是响应里的显式字段，所以不存在"以为成功其实没写"的静默。
        """
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")
        for novel_id in PIXIV_IDS:
            _seed(
                session_factory, novel_id, source="pixiv",
                author_id=AUTHOR_ID, author_name="pixiv 作者",
                series_id=SERIES_ID, series_name="pixiv 系列", series_index=2,
            )
        before = {nid: _snapshot(session_factory, nid) for nid in PIXIV_IDS}

        response = _batch(client, {
            "scope": _ids_scope(*LOCAL_IDS, *PIXIV_IDS),
            "author_id": REMOTE_AUTHOR_ID,
            "series_id": REMOTE_SERIES_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 5, "applicable": 3, "skipped": 2, "affected": 3,
        }
        for novel_id in PIXIV_IDS:
            assert _snapshot(session_factory, novel_id) == before[novel_id], (
                "pixiv 书的作者 / 系列列必须一个字节都没变"
            )
        for novel_id in LOCAL_IDS:
            row = _row(session_factory, novel_id)
            assert (row.author_id, row.author_name) == (
                REMOTE_AUTHOR_ID, "远端作者",
            )
            assert (row.series_id, row.series_name) == (
                REMOTE_SERIES_ID, "远端系列",
            )

    def test_unknown_ids_count_as_skipped_too(self, client, session_factory):
        """选择集里的"库里没有的 id"同样只进 ``skipped``（不报错、不写）。

        批量接口对"过期选择集"（列表页缓存里的 id 已被删）必须幂等可用；
        ``matched`` 是调用方给的整个选择集，所以它们是 ``matched`` 的一部分。
        """
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0], 987_654_321),
            "author_id": REMOTE_AUTHOR_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 2, "applicable": 1, "skipped": 1, "affected": 1,
        }


# ---------------------------------------------------------------------------
# ③ 挂作者：解析只做一次，名字进 author 行与 novel 行
# ---------------------------------------------------------------------------

class TestAssignAuthor:
    def test_local_hit_never_asks_pixiv(self, client, session_factory, pixiv):
        """库里已知的名字不打网络 —— 批量也不例外（离线可用）。"""
        _seed_author(session_factory, AUTHOR_ID, "库里已知的作者")
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")

        response = _batch(client, {
            "scope": _ids_scope(*LOCAL_IDS),
            "author_id": AUTHOR_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 3, "applicable": 3, "skipped": 0, "affected": 3,
        }
        assert pixiv.calls == []
        assert _author(session_factory, AUTHOR_ID).author_name == "库里已知的作者"
        for novel_id in LOCAL_IDS:
            row = _row(session_factory, novel_id)
            assert (row.author_id, row.author_name) == (
                AUTHOR_ID, "库里已知的作者",
            )

    def test_resolution_happens_once_for_the_whole_batch(
        self, client, session_factory, pixiv,
    ):
        """N 本共用**一次**解析（批量相对单本省下的 N-1 次），名字同时规范
        ``author`` 行与同作者的其它 novel 行。"""
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")
        # 占位 author 行（名字待解析）+ 同作者的陈旧副本：解析要落到 pixiv，
        # 名字写回要把 author 行与这本其它作品一起规范（同一作者两个名字是 bug）。
        _seed_author(session_factory, REMOTE_AUTHOR_ID, None)
        _seed(
            session_factory, PIXIV_IDS[0], source="pixiv",
            author_id=REMOTE_AUTHOR_ID, author_name="过时的名字",
        )

        response = _batch(client, {
            "scope": _ids_scope(*LOCAL_IDS),
            "author_id": REMOTE_AUTHOR_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json()["affected"] == 3
        assert pixiv.calls == [REMOTE_AUTHOR_ID], "三本书只解析一次"
        assert _author(session_factory, REMOTE_AUTHOR_ID).author_name == "远端作者"
        assert _row(session_factory, PIXIV_IDS[0]).author_name == "远端作者"
        for novel_id in LOCAL_IDS:
            row = _row(session_factory, novel_id)
            assert (row.author_id, row.author_name) == (
                REMOTE_AUTHOR_ID, "远端作者",
            )


# ---------------------------------------------------------------------------
# ④ 挂系列：只命名 series 行
# ---------------------------------------------------------------------------

class TestAssignSeries:
    def test_series_row_is_named_and_sibling_novels_keep_their_own_names(
        self, client, session_factory, pixiv,
    ):
        """系列名写进这 N 本 + **series 行**；同系列其它 novel 行不被动。

        与作者刻意不同（``repo.update_series_name``）：系列名的规范方向是
        novel → series（``update_summary`` 取 ``max(novel.series_name)``），
        改写同系列其它小说行既没必要也会覆盖来源值。
        """
        _seed_series(session_factory, SERIES_ID, "系列行自己的名字")
        _seed(
            session_factory, PIXIV_IDS[0], source="pixiv",
            author_id=AUTHOR_ID, author_name="pixiv 作者",
            series_id=SERIES_ID, series_name="小说行自己的名字",
        )
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")

        response = _batch(client, {
            "scope": _ids_scope(*LOCAL_IDS),
            "series_id": SERIES_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 3, "applicable": 3, "skipped": 0, "affected": 3,
        }
        assert pixiv.series_calls == [], "series 行有名字 → 本地命中"
        for novel_id in LOCAL_IDS:
            row = _row(session_factory, novel_id)
            assert (row.series_id, row.series_name) == (
                SERIES_ID, "系列行自己的名字",
            )
        assert _series(session_factory, SERIES_ID).series_name == "系列行自己的名字"
        # 同系列的 pixiv 书保留自己的名字：批量编辑没有把它一起"规范"掉
        assert _row(session_factory, PIXIV_IDS[0]).series_name == "小说行自己的名字"

    def test_series_name_falls_back_to_pixiv_once(
        self, client, session_factory, pixiv,
    ):
        """两边都没有 → 问一次 Pixiv，名字写进 novel 行与 series 行。"""
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")

        response = _batch(client, {
            "scope": _ids_scope(*LOCAL_IDS),
            "series_id": REMOTE_SERIES_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert pixiv.series_calls == [REMOTE_SERIES_ID]
        assert _series(session_factory, REMOTE_SERIES_ID).series_name == "远端系列"
        for novel_id in LOCAL_IDS:
            row = _row(session_factory, novel_id)
            assert (row.series_id, row.series_name) == (
                REMOTE_SERIES_ID, "远端系列",
            )


# ---------------------------------------------------------------------------
# ⑤⑥ 解除关联：作者留名（空名补「未知作者」），系列三列一起清
# ---------------------------------------------------------------------------

class TestUnlink:
    def test_unlink_author_keeps_names_and_backfills_only_the_nameless(
        self, client, session_factory,
    ):
        """``author_id=null``：只清 id。

        显示名保留（本地书的名字来自文件名解析，有独立来源）；只有
        ``author_name IS NULL`` 的行补「未知作者」——那是"上一轮解析失败"的
        中间态，留着它这一行就掉出 §3.3「每本书都有作者名可显示」的承诺。
        """
        _seed(
            session_factory, LOCAL_IDS[0],
            author_id=AUTHOR_ID, author_name="文件名解析出的作者",
        )
        _seed(session_factory, LOCAL_IDS[1], author_id=AUTHOR_ID, author_name=None)

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0], LOCAL_IDS[1]),
            "author_id": None,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 2, "applicable": 2, "skipped": 0, "affected": 2,
        }
        kept = _row(session_factory, LOCAL_IDS[0])
        assert kept.author_id is None
        assert kept.author_name == "文件名解析出的作者", "解除关联不清显示名"
        backfilled = _row(session_factory, LOCAL_IDS[1])
        assert backfilled.author_id is None
        assert backfilled.author_name == UNKNOWN_AUTHOR

    def test_unlink_series_clears_the_whole_triple(self, client, session_factory):
        """``series_id=null``：id / 名字 / 章节号是同一个事实，一起进一起出。"""
        _seed(
            session_factory, LOCAL_IDS[0],
            series_id=SERIES_ID, series_name="某个系列", series_index=3,
        )

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]),
            "series_id": None,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json()["affected"] == 1
        row = _row(session_factory, LOCAL_IDS[0])
        assert (row.series_id, row.series_name, row.series_index) == (None, None, None)
        # 系列行本身不受影响：名字是 series 行的，与"这本书挂不挂它"无关
        assert _series(session_factory, SERIES_ID) is not None

    def test_unlink_author_keeps_series_untouched(self, client, session_factory):
        """只提交 ``author_id`` 时系列一列都不动（三态里的"未提交"）。"""
        _seed(
            session_factory, LOCAL_IDS[0],
            author_id=AUTHOR_ID, author_name="本地作者",
            series_id=SERIES_ID, series_name="某个系列", series_index=3,
        )

        assert _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]),
            "author_id": None,
            "apply": True,
        }).status_code == 200

        row = _row(session_factory, LOCAL_IDS[0])
        assert row.author_id is None
        assert (row.series_id, row.series_name, row.series_index) == (
            SERIES_ID, "某个系列", 3,
        )


# ---------------------------------------------------------------------------
# ⑦ 解析失败不致命
# ---------------------------------------------------------------------------

class TestResolutionFailure:
    def test_unreachable_pixiv_still_writes_the_id(
        self, client, session_factory, pixiv,
    ):
        """id 是身份、名字是缓存：拿不到名字照样写，落 NULL 待解析（200）。"""
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")
        pixiv.fail = True

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]),
            "author_id": REMOTE_AUTHOR_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 1, "applicable": 1, "skipped": 0, "affected": 1,
        }
        row = _row(session_factory, LOCAL_IDS[0])
        assert row.author_id == REMOTE_AUTHOR_ID
        assert row.author_name is None, "解析失败 = 待解析，不是失败"
        # 占位 author 行仍要建（外键 + 追更按钮），只是名字待解析
        assert _author(session_factory, REMOTE_AUTHOR_ID) is not None

    def test_unreachable_pixiv_still_writes_the_series_id(
        self, client, session_factory, pixiv,
    ):
        """系列同理：``update_summary`` 下次抓到该系列时会从小说行推导名字。"""
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")
        pixiv.fail = True

        response = _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]),
            "series_id": REMOTE_SERIES_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        row = _row(session_factory, LOCAL_IDS[0])
        assert row.series_id == REMOTE_SERIES_ID
        assert row.series_name is None


# ---------------------------------------------------------------------------
# ⑧ 校验：400（业务规则）与 422（请求体形态）
# ---------------------------------------------------------------------------

class TestValidation:
    @pytest.mark.parametrize("payload,expected", [
        ({"author_id": 0}, 400),          # pixiv 用户 id 从 1 开始
        ({"author_id": -5}, 400),
        ({"series_id": 0}, 400),
        ({"series_id": -5}, 400),
        ({"author_id": True}, 422),       # JSON true 不能变成"1 号作者"
        ({"author_id": 3.5}, 422),
        ({"series_id": False}, 422),
        ({"series_id": "abc"}, 422),
    ])
    def test_ids_must_be_positive_ints(
        self, client, session_factory, payload, expected,
    ):
        """非法 id 的 400 文案口径与单本 PATCH 完全一致（同一条 ``_positive_id``）。"""
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")
        request = {"scope": _ids_scope(LOCAL_IDS[0]), "apply": True, **payload}

        response = _batch(client, request)

        assert response.status_code == expected, response.text
        row = _row(session_factory, LOCAL_IDS[0])
        assert (row.author_id, row.series_id) == (None, None)

    def test_ids_are_rejected_with_the_patch_wording(
        self, client, session_factory,
    ):
        """400 文案是契约（前端直接展示 detail）——与单本逐字相同。"""
        from copixiv.features.novels.update_metadata import _author_id, _series_id
        from copixiv.core.exceptions import ValidationError

        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")
        with pytest.raises(ValidationError) as author_error:
            _author_id(0)
        with pytest.raises(ValidationError) as series_error:
            _series_id(-5)

        assert _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]), "author_id": 0, "apply": True,
        }).json()["detail"] == author_error.value.detail
        assert _batch(client, {
            "scope": _ids_scope(LOCAL_IDS[0]), "series_id": -5, "apply": True,
        }).json()["detail"] == series_error.value.detail

    def test_empty_patch_is_400(self, client, session_factory):
        """三态里的"两个键都不出现" = 没有要改的东西（不是空操作）。"""
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")

        response = _batch(client, {"scope": _ids_scope(LOCAL_IDS[0])})

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "没有要修改的字段"

    def test_empty_scope_is_400(self, client, session_factory):
        """空选择集走 ``resolve_batch_scope``（唯一那条作用域规则）。"""
        response = _batch(client, {
            "scope": _ids_scope(), "author_id": 5, "apply": True,
        })

        assert response.status_code == 400, response.text
        assert "勾选" in response.json()["detail"]

    def test_empty_all_matched_scope_is_404(self, client, session_factory):
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")

        response = _batch(client, {
            "scope": {"mode": "all_matched", "min_like": 999_999},
            "author_id": 5, "apply": True,
        })

        assert response.status_code == 404, response.text

    def test_unknown_field_is_422(self, client, session_factory):
        """``extra="forbid"``：结构性与未知字段都是 422（与 PATCH 同一约定）。"""
        _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")

        for payload in (
            {"unknown_field": "x"},
            {"title": "想直接改标题"},      # 批量编辑这一版不管 title
            {"source": "pixiv"},
            {"series_index": 3},
        ):
            response = _batch(client, {
                "scope": _ids_scope(LOCAL_IDS[0]),
                "author_id": 5,
                "apply": True,
                **payload,
            })
            assert response.status_code == 422, (payload, response.text)

    def test_missing_scope_is_422(self, client):
        assert _batch(client, {"author_id": 5}).status_code == 422


# ---------------------------------------------------------------------------
# ⑨ 幂等：重复提交同一请求
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_repeating_the_same_request_changes_nothing(
        self, client, session_factory, pixiv,
    ):
        """第二次 ``affected=0``：WHERE 里的逐列 ``IS DISTINCT FROM`` 把没有变化的
        行挡在 RETURNING 之外（``add_tags_to_novels`` 同一条规则）。

        重复提交在真实使用里很常见（用户点了两次、前端重试），所以"点两次 =
        点一次"必须是可断言的事实，而不是"值一样所以看不出来"。
        """
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")
        payload = {
            "scope": _ids_scope(*LOCAL_IDS),
            "author_id": REMOTE_AUTHOR_ID,
            "series_id": REMOTE_SERIES_ID,
            "apply": True,
        }

        first = _batch(client, payload)
        assert first.status_code == 200, first.text
        assert first.json()["affected"] == 3
        before = {nid: _snapshot(session_factory, nid) for nid in LOCAL_IDS}

        second = _batch(client, payload)

        assert second.status_code == 200, second.text
        assert second.json()["affected"] == 0
        assert second.json()["applicable"] == 3
        assert {nid: _snapshot(session_factory, nid) for nid in LOCAL_IDS} == before

    def test_preview_twice_is_stable(self, client, session_factory, pixiv):
        """预演本身也是可重复的（相同的 matched/applicable/skipped）。"""
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")
        _seed(
            session_factory, PIXIV_IDS[0], source="pixiv",
            author_id=AUTHOR_ID, author_name="pixiv 作者",
        )
        payload = {
            "scope": _ids_scope(*LOCAL_IDS, PIXIV_IDS[0]),
            "author_id": REMOTE_AUTHOR_ID,
        }

        first = _batch(client, payload).json()
        second = _batch(client, payload).json()

        assert first == second == {
            "matched": 4, "applicable": 3, "skipped": 1, "affected": 0,
        }


# ---------------------------------------------------------------------------
# 作用域：``all_matched`` 走同一个解析器，且**不**吃同步路径的 5000 上限
# ---------------------------------------------------------------------------

class TestScope:
    def test_all_matched_scope_is_not_capped(
        self, client, session_factory, monkeypatch,
    ):
        """``cap=None``：写操作是一条 set-based UPDATE，选择集可以任意大。

        与 ``/batch-task`` 同一口径；同步路径的 5000 上限（``BATCH_MAX_NOVELS``）
        刻意不在这里生效——把它调小也照样通过，正是这条测试的意义。
        """
        from copixiv.features.novels import batch_operations

        monkeypatch.setattr(batch_operations, "BATCH_MAX_NOVELS", 2)
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")
        for novel_id in PIXIV_IDS:
            _seed(
                session_factory, novel_id, source="pixiv",
                author_id=AUTHOR_ID, author_name="pixiv 作者",
            )

        response = _batch(client, {
            "scope": {"mode": "all_matched"},
            "author_id": REMOTE_AUTHOR_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 5, "applicable": 3, "skipped": 2, "affected": 3,
        }

    def test_all_matched_respects_excluded_ids(self, client, session_factory, pixiv):
        """``excluded_ids`` 是"全选后取消几本"——解析出的选择集不含它们。"""
        for novel_id in LOCAL_IDS:
            _seed(session_factory, novel_id, author_name="本地作者")

        response = _batch(client, {
            "scope": {
                "mode": "all_matched",
                "excluded_ids": [LOCAL_IDS[2]],
            },
            "author_id": REMOTE_AUTHOR_ID,
            "apply": True,
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "matched": 2, "applicable": 2, "skipped": 0, "affected": 2,
        }
        assert _row(session_factory, LOCAL_IDS[2]).author_id is None


# ---------------------------------------------------------------------------
# 结构：路由不会被 ``/{novel_id}`` 吃掉（与 ``/batch-export`` 同形）
# ---------------------------------------------------------------------------

def test_route_is_not_captured_by_the_novel_id_routes(client, session_factory):
    """``POST /batch-metadata`` 必须命中批量端点，而不是被当成 ``novel_id``。

    与 ``/batch-export`` 早已是同一种形状（精确路径 + ``POST /{id}/...`` 都要
    第二段）；这条测试把"顺序恰好没事"钉成契约。
    """
    _seed(session_factory, LOCAL_IDS[0], author_name="本地作者")

    response = client.post("/api/novels/batch-metadata", json={
        "scope": _ids_scope(LOCAL_IDS[0]), "author_id": 5,
    })

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"matched", "applicable", "skipped", "affected"}


def test_batch_metadata_is_an_exact_route(client, session_factory):
    """反向确认：批量端点是一条**精确路径**，没有顶掉 ``/{novel_id}/...``。

    ``GET /batch-metadata`` 回 405（路径已知、方法不对）而不是 404，说明路由表里
    它不是某条 ``/{novel_id}`` 的形状；同一次请求里 ``POST /{id}/favourite``
    照旧命中（204），两条路径互不吃掉对方。
    """
    _seed(
        session_factory, PIXIV_IDS[0], source="pixiv",
        author_id=AUTHOR_ID, author_name="pixiv 作者",
    )

    assert client.get("/api/novels/batch-metadata").status_code == 405
    assert client.post(f"/api/novels/{PIXIV_IDS[0]}/favourite").status_code == 204
