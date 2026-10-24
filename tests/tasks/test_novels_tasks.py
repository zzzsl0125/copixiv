"""``tasks/novels.py`` — the adapters between pixiv responses and ``ingest()``.

**Why this file exists at all.**  Until 2026-10-24 this module was the only
task module with no direct tests (31 % line coverage — the worst large module
in the tree).  That was not a coverage accident: every task in here is an
adapter, and the interesting parts are the *early-exit branches* — empty feed,
nothing Chinese in a result page, an author already refreshed today, a
filtered-to-nothing search window.  Those branches are where "the task ran and
found nothing" and "the task never really ran" become indistinguishable to the
operator, so they are exactly the branches worth pinning.

The tests deliberately stop at the adapter boundary:

* the **pixiv client** is a scripted double (it records which methods were
  called and under which account rule);
* :func:`~copixiv.features.novels.ingest.ingest` is replaced by a scripted
  :class:`IngestOutcome` for the discovery tasks, because the pipeline itself
  has its own tests (``tests/features/test_download_novel_use_case.py`` /
  ``test_epub_status_wiring.py``);
* ``author_fetch`` / ``author_delete`` DO run against a real PG database —
  their contract is "what ends up in the tables" (last_update, the author
  row, the delete cascade), and a double would only re-implement the SQL.

``novel_fetch`` is exercised through the **real** ingest pipeline for the same
reason: its summary is a claim about what ``ingest`` did, and a stubbed
``IngestOutcome`` would let the claim drift away from the behaviour without any
test noticing.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

import copixiv.tasks.novels as novels_mod
from copixiv.core.models import TaskResult
from copixiv.db.models import Author, FailedNovel, Novel, Series
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import IngestOutcome
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.novels import (
    AuthorDeleteArgs,
    AuthorFetchArgs,
    FailedRetryArgs,
    NovelFetchArgs,
    NovelFollowArgs,
    NovelRankingArgs,
    NovelSearchArgs,
    _batch_summary,
    _fan_out_author_fetch,
    author_delete,
    author_fetch,
    author_special_follow,
    failed_retry,
    novel_fetch,
    novel_follow,
    novel_ranking,
    novel_search,
)

AUTHOR_NAME = "测试作者"


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Every test starts from an empty database (PG fixture from conftest)."""
    yield


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class ScriptedClient:
    """A pixiv client that answers from a script and records the calls.

    ``account_rule`` is a real async context manager, not a no-op: several
    tasks *must* pin an account rule (``force_follow`` for the following feed,
    ``need_premium`` for search), and asserting that is part of the contract.
    """

    def __init__(self, *, webview=None, user_novels=None, user_detail=None):
        self._webview = webview
        self._user_novels = user_novels or {}
        self._user_detail = user_detail
        self.calls: list[tuple] = []
        self.rules: list[dict] = []

    @asynccontextmanager
    async def account_rule(self, **kwargs):
        self.rules.append(kwargs)
        yield self

    async def webview_novel(self, novel_id):
        self.calls.append(("webview_novel", novel_id))
        return self._webview

    async def user_novels(self, author_id, fetch_all=False):
        self.calls.append(("user_novels", author_id, fetch_all))
        return self._user_novels.get(author_id, {"novels": []})

    async def user_detail(self, user_id):
        self.calls.append(("user_detail", user_id))
        if self._user_detail is not None:
            return self._user_detail
        return {"user": {"name": AUTHOR_NAME}}

    async def user_follow_add(self, user_id):
        self.calls.append(("user_follow_add", user_id))
        return {}

    async def user_follow_delete(self, user_id):
        self.calls.append(("user_follow_delete", user_id))
        return {}

    async def novel_follow(self, fetch_til=None):
        self.calls.append(("novel_follow", fetch_til))
        return self._feed

    async def novel_ranking(self, mode="day_r18", date=None, fetch_all=False):
        self.calls.append(("novel_ranking", mode, date, fetch_all))
        return self._ranking

    async def search_novel(self, keyword, search_type=None, order=None,
                           start_date=None, end_date=None,
                           fetch_minlike=None):
        self.calls.append(("search_novel", keyword, start_date, end_date))
        return self._search

    # Script setters keep construction readable at each call site.
    def feed(self, resp):
        self._feed = resp
        return self

    def ranking(self, resp=None):
        """The scripted ``novel_ranking`` response (one page, any date)."""
        self._ranking = {"novels": []} if resp is None else resp
        return self

    def search(self, resp=None):
        """The scripted ``search_novel`` response (one page, any window)."""
        self._search = {"novels": []} if resp is None else resp
        return self

    def named(self, *names):
        """Assert helper: the method names called, in order."""
        return [c[0] for c in self.calls]


class FakeImageDownloader:
    """A downloader whose asset work is already complete (no real files)."""

    async def process_novel_assets(self, draft, force=False):
        return None

    async def await_all(self, ids=None):
        return []

    def drain_outcomes(self, ids=None):
        return {}


def novel_info(nid: int, *, author_id=1, title="中文新作", tags=None):
    """One ``novelInfo``-shaped object as the discovery endpoints return it.

    ``tags`` defaults to an explicit Chinese tag, which is how a real Chinese
    work is usually labelled.  Pass ``tags=()`` when the *point* of the case is
    that no Chinese signal exists — an explicit tag wins outright in
    ``is_chinese``, so leaving it on would quietly make every case Chinese and
    the Japanese cases would prove nothing.
    """
    tag_names = ("中文",) if tags is None else tags
    return SimpleNamespace(
        id=nid,
        title=title,
        caption="中文简介",
        tags=[SimpleNamespace(name=t) for t in tag_names],
        user=SimpleNamespace(id=author_id, name=AUTHOR_NAME),
        series=None,
        total_bookmarks=10,
        total_view=100,
        text_length=50,
        create_date="2026-10-01T00:00:00",
    )


def webview_payload(nid: int, *, title="新小说", body="正文内容"):
    """One ``webview_novel`` payload (the single-novel path's shape)."""
    return SimpleNamespace(
        id=nid,
        title=title,
        user_id=1,
        rating=SimpleNamespace(bookmark=5, view=10),
        text=body,
        caption="中文标题",
        series_id=None,
        series_title=None,
        series_navigation=None,
        cdate="2026-01-01T00:00:00",
        tags=["中文"],
        images=None,
        illusts=None,
        cover_url=None,
    )


def make_ctx(session_factory, client, *, storage=None, downloader=None,
             uow=None) -> TaskContext:
    return TaskContext(
        uow=uow or SqlUnitOfWork(session_factory),
        session_factory=session_factory,
        client=client,
        file_storage=storage,
        image_downloader=downloader or FakeImageDownloader(),
    )


def outcome(titles=(), failed=(), new_count=None) -> IngestOutcome:
    """A scripted :class:`IngestOutcome` for the discovery-task tests."""
    return IngestOutcome(
        titles=list(titles),
        failed=[(nid, "boom") for nid in failed],
        new_count=len(titles) if new_count is None else new_count,
    )


@pytest.fixture
def stub_ingest(monkeypatch):
    """Replace ``tasks/novels``' ``ingest`` with a recording scripted double.

    The *module attribute* is patched on purpose: every adapter calls ``ingest``
    by its module-level name, so patching the module is what the fan-out and the
    per-day loops actually resolve.
    """
    calls: list[dict] = []

    def install(result=None):
        async def _ingest(novels=None, *, ids=None, force=False, **kwargs):
            calls.append({
                "ids": None if ids is None else list(ids),
                "novels": None if novels is None else list(novels),
                "force": force,
            })
            return result if result is not None else IngestOutcome()
        monkeypatch.setattr(novels_mod, "ingest", _ingest)
        return calls

    return install


def seed_author(session_factory, author_id=1, *, name=AUTHOR_NAME,
                special=False, last_update=None):
    with session_factory() as s:
        s.add(Author(
            author_id=author_id, author_name=name,
            is_special_follow=special, last_update=last_update,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# The shared one-line summary
# ---------------------------------------------------------------------------


class TestBatchSummary:
    def test_clean_run_says_only_the_counts(self):
        assert _batch_summary("关注更新", outcome(titles=["a", "b"])) == (
            "关注更新: 新增 2 本小说"
        )

    def test_redownloads_are_counted_as_updates_not_as_new(self):
        """``force=True`` fetches existing novels; they are not 「新增」.

        Measured before the fix: re-downloading one existing novel with
        ``force=True`` produced ``new_count == 0`` and ``titles == ["书"]``,
        and this helper reported 「新增 1 本小说」 for zero inserted rows.
        """
        out = outcome(titles=["书"], new_count=0)
        assert _batch_summary("排行榜", out) == "排行榜: 新增 0 本小说，更新 1 本"

    def test_mixed_new_and_refreshed_are_both_counted(self):
        out = outcome(titles=["新书", "老书"], new_count=1)
        assert _batch_summary("作者 #1", out) == (
            "作者 #1: 新增 1 本小说，更新 1 本"
        )

    def test_partial_failure_is_visible_in_the_line(self):
        """A half-failed batch must not read like a clean one.

        ``summary`` is what the operator and the Telegram notifier show; the
        machine-readable ids ride on ``failed_ids``.  If the wording drops the
        failure count, a broken night looks like a quiet one.
        """
        summary = _batch_summary("关注更新", outcome(titles=["a"], failed=[7, 8]))
        assert summary == "关注更新: 新增 1 本小说，失败 2 本"

    def test_failure_that_deduped_ids_hide_is_still_counted(self):
        """``IngestOutcome.failed`` may name one id twice; the count follows ids.

        ``failed_ids`` dedupes (the same novel can fail download *and* asset
        processing), so the suffix counts unique ids, not raw records.
        """
        out = IngestOutcome(failed=[(9, "download"), (9, "assets")])
        assert _batch_summary("特别关注", out) == "特别关注: 新增 0 本小说，失败 1 本"


# ---------------------------------------------------------------------------
# _fan_out_author_fetch — the shared fan-out of ranking / search
# ---------------------------------------------------------------------------


class TestFanOutAuthorFetch:
    async def test_no_novels_means_no_client_traffic(self, session_factory):
        client = ScriptedClient()
        titles, failed = await _fan_out_author_fetch(
            [], force=False, ctx=make_ctx(session_factory, client),
        )
        assert (titles, failed) == ([], [])
        assert client.calls == []
        assert client.rules == []

    async def test_non_chinese_page_is_dropped_before_any_fan_out(
        self, session_factory, stub_ingest,
    ):
        """Ranking pages are full of Japanese works; none may reach an author."""
        calls = stub_ingest()
        client = ScriptedClient()
        japanese = novel_info(1, title="日本語の小説", tags=())   # kana → rejected

        titles, failed = await _fan_out_author_fetch(
            [japanese], force=False, ctx=make_ctx(session_factory, client),
        )

        assert (titles, failed) == ([], [])
        assert client.rules == [], "过滤后为空不该进入 account_rule / gather"
        assert calls == []

    async def test_chinese_page_without_user_objects_is_dropped(
        self, session_factory, stub_ingest,
    ):
        """A payload without ``user`` cannot name an author to fetch."""
        calls = stub_ingest()
        client = ScriptedClient()
        no_user = SimpleNamespace(id=1, title="中文新作", caption="中文简介",
                                 tags=[SimpleNamespace(name="中文")])

        titles, failed = await _fan_out_author_fetch(
            [no_user], force=False, ctx=make_ctx(session_factory, client),
        )

        assert (titles, failed) == ([], [])
        assert calls == []

    async def test_unique_authors_are_fetched_once_under_one_account_rule(
        self, session_factory, monkeypatch,
    ):
        """Two novels by the same author must not fetch that author twice."""
        import asyncio

        seen: list[int] = []

        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            seen.append(args.author_id)
            await asyncio.sleep(0)                   # force interleaving
            return TaskResult(
                summary="ok", new_novel_titles=[f"t{args.author_id}"],
            )

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient()

        titles, failed = await _fan_out_author_fetch(
            [novel_info(1, author_id=11), novel_info(2, author_id=11),
             novel_info(3, author_id=22)],
            force=True, ctx=make_ctx(session_factory, client),
        )

        assert sorted(seen) == [11, 22]
        assert titles == ["t11", "t22"]
        assert failed == []
        assert client.rules == [{}]   # exactly one rule scope for the whole batch

    async def test_child_failures_travel_up(self, session_factory, monkeypatch):
        """A failed author must not leave the day looking clean."""

        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            if args.author_id == 22:
                return TaskResult(summary="partial", failed_ids=[7, 8])
            return TaskResult(summary="ok", new_novel_titles=["ok"])

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient()

        titles, failed = await _fan_out_author_fetch(
            [novel_info(1, author_id=11), novel_info(2, author_id=22)],
            force=False, ctx=make_ctx(session_factory, client),
        )

        assert titles == ["ok"]
        assert failed == [7, 8]

    async def test_a_non_taskresult_child_is_ignored_not_trusted(
        self, session_factory, monkeypatch,
    ):
        """Only a real ``TaskResult`` carries titles / failures.

        ``author_fetch`` is documented to return one; anything else is a
        contract break, and the fan-out must not read attributes off it (which
        would ``AttributeError`` and take the whole ranking day down).
        """
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return None

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        titles, failed = await _fan_out_author_fetch(
            [novel_info(1, author_id=11)], force=False,
            ctx=make_ctx(session_factory, ScriptedClient()),
        )
        assert (titles, failed) == ([], [])


# ---------------------------------------------------------------------------
# novel_fetch — the single-novel path, through the real ingest pipeline
# ---------------------------------------------------------------------------


class TestNovelFetch:
    async def test_new_novel_reports_the_download(
        self, session_factory, tmp_path,
    ):
        storage = FileStorage(str(tmp_path / "download"))
        client = ScriptedClient(webview=webview_payload(501))
        ctx = make_ctx(session_factory, client, storage=storage)

        result = await novel_fetch(NovelFetchArgs(id=501), ctx)

        assert result.summary == "下载完成: 新小说"
        assert result.new_novel_titles == ["新小说"]
        assert result.failed_ids == []
        with session_factory() as s:
            assert s.get(Novel, 501) is not None

    async def test_missing_novel_is_a_failure_with_its_id(
        self, session_factory, tmp_path,
    ):
        """A deleted / restricted work must carry the id, not just a sentence."""
        storage = FileStorage(str(tmp_path / "download"))
        client = ScriptedClient(webview=None)
        ctx = make_ctx(session_factory, client, storage=storage)

        result = await novel_fetch(NovelFetchArgs(id=502, redownload=True), ctx)

        assert result.summary == "小说 #502 获取失败"
        assert result.failed_ids == [502]
        assert result.new_novel_titles == []

    async def test_unchanged_redownload_is_reported_as_skipped(
        self, session_factory, tmp_path,
    ):
        """Body already identical → nothing written → say so."""
        storage = FileStorage(str(tmp_path / "download"))
        storage.save_novel_text(501, "新小说", "正文内容", force=True)
        seed_author(session_factory, 1)
        with session_factory() as s:
            s.add(Novel(
                id=501, title="新小说", author_id=1, like=5, text=4,
                shuffle=1, has_epub=3,
                path=str(storage.novel_text_path(501, "新小说")),
            ))
            s.commit()
        client = ScriptedClient(webview=webview_payload(501))
        ctx = make_ctx(session_factory, client, storage=storage)

        result = await novel_fetch(NovelFetchArgs(id=501, redownload=False), ctx)

        assert result.summary == "已存在，跳过"
        assert "下载完成" not in result.summary
        assert result.failed_ids == []
        # Nothing was newly persisted, so the TaskResult contract ("titles of
        # novels newly discovered and persisted") says: don't claim one.
        assert result.new_novel_titles == []
        assert result.new_novel_count == 0

    async def test_redownload_that_rewrote_the_body_does_not_claim_to_skip(
        self, session_factory, tmp_path,
    ):
        """The summary must not say "skipped" when this run replaced the text.

        Before 2026-10-24 the branch was ``out.titles and out.new_count``: a
        re-download of an already-known novel always has ``titles`` (there is a
        fresh body) but ``new_count == 0`` (no row inserted), so a run that
        rewrote the body and rebuilt the EPUB still reported 「已存在，跳过」.
        The operator reads that line to decide whether the re-download worked.
        """
        storage = FileStorage(str(tmp_path / "download"))
        storage.save_novel_text(501, "新小说", "旧的正文", force=True)
        seed_author(session_factory, 1)
        with session_factory() as s:
            s.add(Novel(
                id=501, title="新小说", author_id=1, like=5, text=4,
                shuffle=1, has_epub=3,
                path=str(storage.novel_text_path(501, "新小说")),
            ))
            s.commit()
        client = ScriptedClient(webview=webview_payload(501, body="全新的正文"))
        ctx = make_ctx(session_factory, client, storage=storage)

        result = await novel_fetch(NovelFetchArgs(id=501, redownload=True), ctx)

        assert "跳过" not in result.summary
        assert result.summary == "已更新: 新小说"
        # The body really was replaced — otherwise this test proves nothing.
        assert storage.read_novel_text(501, "新小说") == "全新的正文"
        assert result.new_novel_titles == [], "没有新行，就不该报新书"

    async def test_a_run_that_fetched_nothing_is_not_reported_as_skipped(
        self, session_factory, tmp_path,
    ):
        """No client ⇒ no fetch ⇒ must not read as 「已存在，跳过」.

        A degraded assembly (client omitted) made ``ingest`` skip the download
        silently, and this adapter's summary said 「已存在，跳过」 — a task row
        reading *success* for a novel that was never requested, with no row in
        the database.  The pipeline now records those ids as failures, and the
        adapter refuses to claim "already have it" when nothing was fetched.
        """
        storage = FileStorage(str(tmp_path / "download"))
        ctx = TaskContext(
            uow=SqlUnitOfWork(session_factory), session_factory=session_factory,
            client=None, file_storage=storage,
            image_downloader=FakeImageDownloader(),
        )

        result = await novel_fetch(NovelFetchArgs(id=504), ctx)

        assert "跳过" not in result.summary
        assert result.failed_ids == [504]
        with session_factory() as s:
            assert s.get(Novel, 504) is None

    async def test_the_ingest_failure_flag_sets_the_id(
        self, session_factory, tmp_path, monkeypatch,
    ):
        """``failed_ids`` is what callers read; it must follow ``out.failed``."""
        storage = FileStorage(str(tmp_path / "download"))

        async def _failing_ingest(**kwargs):
            return IngestOutcome(failed=[(503, "asset 处理失败")])

        monkeypatch.setattr(novels_mod, "ingest", _failing_ingest)
        result = await novel_fetch(
            NovelFetchArgs(id=503),
            make_ctx(session_factory, ScriptedClient(), storage=storage),
        )

        assert result.summary == "小说 #503 获取失败"
        assert result.failed_ids == [503]


    async def test_pipeline_silence_about_a_download_is_not_success(
        self, session_factory, tmp_path, monkeypatch,
    ):
        """If ``ingest`` returns nothing fetched *and* no failure, say so.

        ``novel_fetch`` reports what the pipeline did; when the pipeline
        neither fetched nor blamed anything (an unforeseen early exit), the
        adapter must not paper over it with 「已存在，跳过」.  Asserted against a
        scripted outcome so the branch is reachable independently of whatever
        causes the silence.
        """
        storage = FileStorage(str(tmp_path / "download"))

        async def _silent_ingest(**kwargs):
            return IngestOutcome()

        monkeypatch.setattr(novels_mod, "ingest", _silent_ingest)
        result = await novel_fetch(
            NovelFetchArgs(id=505),
            make_ctx(session_factory, ScriptedClient(), storage=storage),
        )

        assert result.summary == "小说 #505 获取失败（未取到内容）"
        assert result.failed_ids == [505]


# ---------------------------------------------------------------------------
# novel_follow — the following feed
# ---------------------------------------------------------------------------


class TestNovelFollow:
    async def test_the_feed_is_fetched_on_the_following_account(
        self, session_factory, stub_ingest,
    ):
        """pixiv scopes ``novel_follow`` to the authenticated account's list."""
        calls = stub_ingest(outcome(titles=["新书"]))
        client = ScriptedClient()
        client.feed({"novels": [novel_info(601)]})

        result = await novel_follow(NovelFollowArgs(), make_ctx(session_factory, client))

        assert client.rules == [{"force_follow": True}]
        assert [c[0] for c in client.calls] == ["novel_follow"]
        assert calls[0]["novels"] is not None, "收集到的列表必须交给 ingest"
        assert result.summary == "关注更新: 新增 1 本小说"

    async def test_an_empty_feed_still_produces_a_result(
        self, session_factory, stub_ingest,
    ):
        calls = stub_ingest()
        client = ScriptedClient().feed({"novels": []})

        result = await novel_follow(NovelFollowArgs(days=7), make_ctx(session_factory, client))

        assert calls[0]["novels"] == []
        assert result.summary == "关注更新: 新增 0 本小说"
        assert result.new_novel_titles == []

    async def test_a_missing_response_is_treated_as_an_empty_feed(
        self, session_factory, stub_ingest,
    ):
        """The client returns ``None`` when the vendor response is empty."""
        calls = stub_ingest()
        client = ScriptedClient().feed(None)

        result = await novel_follow(NovelFollowArgs(), make_ctx(session_factory, client))

        assert calls[0]["novels"] == []
        assert result.summary == "关注更新: 新增 0 本小说"

    async def test_japanese_works_never_reach_ingest(
        self, session_factory, stub_ingest,
    ):
        calls = stub_ingest()
        client = ScriptedClient().feed({
            "novels": [
                novel_info(602, title="日本語のタイトル", tags=()),
                novel_info(603, title="中文标题"),
            ],
        })

        await novel_follow(NovelFollowArgs(), make_ctx(session_factory, client))

        assert [n.id for n in calls[0]["novels"]] == [603]

    async def test_force_is_forwarded_to_ingest(self, session_factory, stub_ingest):
        calls = stub_ingest()
        client = ScriptedClient().feed({"novels": [novel_info(604)]})

        await novel_follow(NovelFollowArgs(force=True), make_ctx(session_factory, client))

        assert calls[0]["force"] is True

    async def test_partial_failure_is_reported_not_hidden(
        self, session_factory, stub_ingest,
    ):
        stub_ingest(outcome(titles=["好的"], failed=[605]))
        client = ScriptedClient().feed({"novels": [novel_info(605)]})

        result = await novel_follow(NovelFollowArgs(), make_ctx(session_factory, client))

        assert result.failed_ids == [605]
        assert result.summary.endswith("失败 1 本")


# ---------------------------------------------------------------------------
# author_fetch — the author catalogue
# ---------------------------------------------------------------------------


class TestAuthorFetch:
    async def test_an_author_updated_today_is_skipped_without_network(
        self, session_factory,
    ):
        from datetime import datetime, timezone

        seed_author(session_factory, 1, last_update=datetime.now(timezone.utc))
        client = ScriptedClient()

        result = await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert result.summary == "作者 #1 今日已更新，跳过"
        assert result.new_novel_titles == []
        # The whole point of the skip: no API traffic at all.
        assert client.calls == []

    async def test_force_overrides_the_today_skip(
        self, session_factory, stub_ingest,
    ):
        from datetime import datetime, timezone

        seed_author(session_factory, 1, last_update=datetime.now(timezone.utc))
        calls = stub_ingest(outcome(titles=["新章"]))
        client = ScriptedClient(user_novels={1: {"novels": [novel_info(701)]}})

        result = await author_fetch(
            AuthorFetchArgs(author_id=1, force=True), make_ctx(session_factory, client),
        )

        assert ("user_novels", 1, True) in client.calls
        assert result.summary == "作者 测试作者: 新增 1 本小说"
        assert calls[0]["novels"] is not None

    async def test_an_unknown_author_is_followed_then_fetched(
        self, session_factory, stub_ingest,
    ):
        """Following the author is what makes the daily follow feed work.

        ``novel_follow`` only returns works of accounts the authenticated user
        follows, so an author that is not in the local table must be followed
        here — this is the entry point of the whole 追更 chain, not a side
        effect to be removed (docs/TASK_SYSTEM_REVIEW-2026-10-23.md P1-16).
        """
        stub_ingest(outcome(titles=["新章"]))
        client = ScriptedClient(user_novels={1: {"novels": [novel_info(701)]}})

        # Deliberately no author row: this is the "stranger" case.
        await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert ("user_follow_add", 1) in client.calls
        assert client.rules == [{"force_follow": True}]   # the follow call's rule

    async def test_a_known_author_is_not_followed_again(
        self, session_factory, stub_ingest,
    ):
        seed_author(session_factory, 1)
        stub_ingest()
        client = ScriptedClient(user_novels={1: {"novels": []}})

        await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert ("user_follow_add", 1) not in client.calls

    async def test_last_update_is_stamped_even_when_nothing_was_found(
        self, session_factory, stub_ingest,
    ):
        """Otherwise every run re-fetches the same quiet author all day."""
        from datetime import datetime, timezone

        seed_author(session_factory, 1, last_update=None)
        stub_ingest()
        client = ScriptedClient(user_novels={1: {"novels": []}})

        await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        with session_factory() as s:
            author = s.get(Author, 1)
            assert author.last_update is not None
            assert (datetime.now(timezone.utc) - author.last_update).days == 0

    async def test_the_resolved_name_labels_the_summary(
        self, session_factory, stub_ingest,
    ):
        """The webview API never returns the author name; ``user_detail`` does."""
        seed_author(session_factory, 1, name=None)
        stub_ingest(outcome(titles=["新章"]))
        client = ScriptedClient(
            user_novels={1: {"novels": [novel_info(701)]}},
            user_detail={"user": {"name": "另一位作者"}},
        )

        result = await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert result.summary == "作者 另一位作者: 新增 1 本小说"
        with session_factory() as s:
            assert s.get(Author, 1).author_name == "另一位作者"

    async def test_an_unresolvable_name_falls_back_to_the_id(
        self, session_factory, stub_ingest,
    ):
        seed_author(session_factory, 1, name=None)
        stub_ingest()
        client = ScriptedClient(
            user_novels={1: {"novels": []}},
            user_detail={"user": {"name": ""}},
        )

        result = await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert result.summary == "作者 #1: 新增 0 本小说"

    async def test_redownload_flag_reaches_ingest(
        self, session_factory, stub_ingest,
    ):
        seed_author(session_factory, 1)
        calls = stub_ingest()
        client = ScriptedClient(user_novels={1: {"novels": [novel_info(701)]}})

        await author_fetch(
            AuthorFetchArgs(author_id=1, redownload=True),
            make_ctx(session_factory, client),
        )

        assert calls[0]["force"] is True

    async def test_partial_failure_keeps_the_author_label(
        self, session_factory, stub_ingest,
    ):
        seed_author(session_factory, 1)
        stub_ingest(outcome(titles=[], failed=[701]))
        client = ScriptedClient(user_novels={1: {"novels": []}})

        result = await author_fetch(
            AuthorFetchArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert result.failed_ids == [701]
        assert result.summary == "作者 测试作者: 新增 0 本小说，失败 1 本"


# ---------------------------------------------------------------------------
# author_delete
# ---------------------------------------------------------------------------


class TestAuthorDelete:
    async def test_delete_removes_the_author_their_novels_and_series(
        self, session_factory,
    ):
        with session_factory() as s:
            s.add(Series(series_id=90, series_name="系列", author_id=1))
            s.add(Author(author_id=1, author_name=AUTHOR_NAME))
            s.flush()
            s.add(Novel(id=801, title="书", author_id=1, series_id=90,
                        like=1, text=1, shuffle=1))
            s.commit()
        client = ScriptedClient()

        result = await author_delete(
            AuthorDeleteArgs(author_id=1), make_ctx(session_factory, client),
        )

        assert result.summary == "已删除作者 #1 及其全部小说"
        with session_factory() as s:
            assert s.get(Author, 1) is None
            assert s.get(Novel, 801) is None
            assert s.get(Series, 90) is None
            # The ledger row has no FK, so it must be cleaned explicitly.
            assert s.execute(select(FailedNovel)).scalars().all() == []
        assert ("user_follow_delete", 1) in client.calls

    async def test_unfollowing_uses_the_following_account(self, session_factory):
        """Unfollow must target the same account whose list was populated."""
        seed_author(session_factory, 1)
        client = ScriptedClient()

        await author_delete(AuthorDeleteArgs(author_id=1),
                            make_ctx(session_factory, client))

        assert client.rules == [{"force_follow": True}]


# ---------------------------------------------------------------------------
# author_special_follow
# ---------------------------------------------------------------------------


class TestAuthorSpecialFollow:
    async def test_no_specially_followed_author_is_a_clean_no_op(
        self, session_factory, stub_ingest,
    ):
        calls = stub_ingest()
        client = ScriptedClient()

        result = await author_special_follow(make_ctx(session_factory, client))

        assert result.summary == "无特别关注的作者"
        assert client.calls == []
        assert calls == []

    async def test_each_special_author_is_fetched_and_merged(
        self, session_factory, stub_ingest,
    ):
        seed_author(session_factory, 1, special=True)
        seed_author(session_factory, 2, special=True)
        seed_author(session_factory, 3, special=False)     # must be ignored
        calls = stub_ingest(outcome(titles=["a", "b"]))
        client = ScriptedClient(user_novels={
            1: {"novels": [novel_info(901)]},
            2: {"novels": [novel_info(902)]},
        })

        result = await author_special_follow(make_ctx(session_factory, client))

        fetched = sorted(c[1] for c in client.calls if c[0] == "user_novels")
        assert fetched == [1, 2]
        assert {n.id for n in calls[0]["novels"]} == {901, 902}
        assert result.summary == "特别关注: 新增 2 本小说"

    async def test_one_failing_author_does_not_lose_the_others(
        self, session_factory, stub_ingest,
    ):
        """``return_exceptions=True`` is the whole reason this task is robust."""
        seed_author(session_factory, 1, special=True)
        seed_author(session_factory, 2, special=True)
        calls = stub_ingest(outcome(titles=["好的"]))
        client = ScriptedClient(user_novels={1: {"novels": [novel_info(901)]}})

        healthy = client.user_novels

        async def _flaky(author_id, fetch_all=False):
            if author_id == 2:
                client.calls.append(("user_novels", author_id, fetch_all))
                raise RuntimeError("author 2 挂了")
            return await healthy(author_id, fetch_all)

        client.user_novels = _flaky

        result = await author_special_follow(make_ctx(session_factory, client))

        assert {n.id for n in calls[0]["novels"]} == {901}
        assert result.summary == "特别关注: 新增 1 本小说"

    async def test_japanese_works_from_followed_authors_are_filtered(
        self, session_factory, stub_ingest,
    ):
        """This task used to import whatever the authors published (1,528 rows)."""
        seed_author(session_factory, 1, special=True)
        calls = stub_ingest()
        client = ScriptedClient(user_novels={1: {"novels": [
            novel_info(901, title="日本語の作品", tags=()),
            novel_info(902, title="中文作品"),
        ]}})

        await author_special_follow(make_ctx(session_factory, client))

        assert [n.id for n in calls[0]["novels"]] == [902]

    async def test_an_empty_catalogue_still_reaches_a_result(
        self, session_factory, stub_ingest,
    ):
        seed_author(session_factory, 1, special=True)
        calls = stub_ingest()
        client = ScriptedClient(user_novels={1: {"novels": []}})

        result = await author_special_follow(make_ctx(session_factory, client))

        assert calls[0]["novels"] == []
        assert result.summary == "特别关注: 新增 0 本小说"


# ---------------------------------------------------------------------------
# novel_ranking
# ---------------------------------------------------------------------------


class TestNovelRanking:
    async def test_every_requested_day_is_queried(self, session_factory, monkeypatch):
        days: list = []

        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return TaskResult(summary="ok")

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().ranking()

        await novel_ranking(
            NovelRankingArgs(mode="week", days=3), make_ctx(session_factory, client),
        )

        ranking_calls = [c for c in client.calls if c[0] == "novel_ranking"]
        assert len(ranking_calls) == 3
        assert all(c[1] == "week" and c[3] is True for c in ranking_calls)
        days.extend(c[2] for c in ranking_calls)
        # Yesterday first, then further back — the day's own ranking is
        # incomplete and is skipped by design.
        assert days == sorted(days, reverse=True)

    async def test_empty_days_report_zero_without_fanning_out(
        self, session_factory, monkeypatch,
    ):
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            raise AssertionError("空榜单不该扇出任何作者")

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().ranking()

        result = await novel_ranking(
            NovelRankingArgs(mode="day_r18", days=2),
            make_ctx(session_factory, client),
        )

        assert result.summary == "排行榜 (day_r18): 新增 0 本小说"
        assert result.new_novel_titles == []
        assert result.failed_ids == []

    async def test_titles_from_every_day_accumulate(
        self, session_factory, monkeypatch,
    ):
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return TaskResult(
                summary="ok", new_novel_titles=[f"书{args.author_id}"],
            )

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        # Every day names the same two authors.
        client = ScriptedClient().ranking(_authors_page(11, 22))

        result = await novel_ranking(
            NovelRankingArgs(days=2), make_ctx(session_factory, client),
        )

        assert result.new_novel_titles == ["书11", "书22", "书11", "书22"]

    async def test_failed_ids_are_deduped_across_days(
        self, session_factory, monkeypatch,
    ):
        """The same novel can rank on two days; it is one failure, not two."""
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return TaskResult(summary="failed", failed_ids=[7])

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().ranking(_authors_page(11, 22))

        result = await novel_ranking(
            NovelRankingArgs(days=2), make_ctx(session_factory, client),
        )

        assert result.failed_ids == [7]
        assert result.summary == "排行榜 (day_r18): 新增 0 本小说，失败 1 本"


def _authors_page(*author_ids) -> dict:
    """One response page naming one novel per given author.

    Both ``novel_ranking`` and ``search_novel`` return the same envelope, and
    the tasks read ``resp["novels"]``; returning one page for every date /
    window is what a real single-page day looks like.
    """
    return {"novels": [
        novel_info(i, author_id=aid) for i, aid in enumerate(author_ids, start=1)
    ]}


# ---------------------------------------------------------------------------
# novel_search
# ---------------------------------------------------------------------------


class TestNovelSearch:
    async def test_search_requires_a_premium_account(self, session_factory, monkeypatch):
        """Popularity ordering needs premium; the rule must be requested."""
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return TaskResult(summary="ok")

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().search()

        await novel_search(NovelSearchArgs(months=1), make_ctx(session_factory, client))

        assert client.rules == [{"need_premium": True}]

    async def test_one_window_per_requested_month(self, session_factory, monkeypatch):
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return TaskResult(summary="ok")

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().search()

        await novel_search(
            NovelSearchArgs(keyword="R-18", months=3),
            make_ctx(session_factory, client),
        )

        searches = [c for c in client.calls if c[0] == "search_novel"]
        assert len(searches) == 3
        assert all(c[1] == "R-18" for c in searches)
        # Windows walk backwards, never overlapping.
        assert [c[2] for c in searches] == sorted(
            (c[2] for c in searches), reverse=True,
        )

    async def test_an_empty_month_does_not_fan_out(
        self, session_factory, monkeypatch,
    ):
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            raise AssertionError("空搜索结果不该扇出任何作者")

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().search()

        result = await novel_search(NovelSearchArgs(months=1),
                                    make_ctx(session_factory, client))

        assert result.summary == "搜索 (R-18): 新增 0 本小说"
        assert result.failed_ids == []

    async def test_fan_out_runs_inside_the_premium_scope(
        self, session_factory, monkeypatch,
    ):
        """The author catalogue fetch happens *inside* the account rule.

        ``novel_search`` opens one ``account_rule(need_premium=True)`` around
        the whole loop, so the nested ``author_fetch`` fan-out must run under
        it rather than re-selecting an account per author.
        """
        rules_seen: list[dict] = []

        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            rules_seen.extend(ctx.client.rules)
            return TaskResult(summary="ok", new_novel_titles=["书"])

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().search(_authors_page(11))

        await novel_search(NovelSearchArgs(months=1), make_ctx(session_factory, client))

        assert rules_seen, "扇出必须在 account_rule 作用域内"
        assert client.rules[0] == {"need_premium": True}

    async def test_failed_ids_are_deduped_across_months(
        self, session_factory, monkeypatch,
    ):
        async def _fake_author_fetch(args, ctx):     # noqa: ARG001
            return TaskResult(summary="failed", failed_ids=[5, 5])

        monkeypatch.setattr(novels_mod, "author_fetch", _fake_author_fetch)
        client = ScriptedClient().search(_authors_page(11))

        result = await novel_search(NovelSearchArgs(months=2),
                                    make_ctx(session_factory, client))

        assert result.failed_ids == [5]
        assert result.summary == "搜索 (R-18): 新增 0 本小说，失败 1 本"


# ---------------------------------------------------------------------------
# failed_retry
# ---------------------------------------------------------------------------


class TestFailedRetry:
    async def test_empty_list_is_a_no_op(self, session_factory, monkeypatch):
        async def _never(args, ctx):                 # noqa: ARG001
            raise AssertionError("空重试列表不该发任何请求")

        monkeypatch.setattr(novels_mod, "novel_fetch", _never)
        result = await failed_retry(
            FailedRetryArgs(novel_ids=[]), make_ctx(session_factory, ScriptedClient()),
        )

        assert result.summary == "重试列表为空"
        assert result.new_novel_titles == []

    async def test_duplicate_ids_are_retried_once(self, session_factory, monkeypatch):
        seen: list[int] = []

        async def _fake_fetch(args, ctx):            # noqa: ARG001
            seen.append(args.id)
            return TaskResult(summary="ok", new_novel_titles=["ok"])

        monkeypatch.setattr(novels_mod, "novel_fetch", _fake_fetch)
        result = await failed_retry(
            FailedRetryArgs(novel_ids=[1, 1, 2]),
            make_ctx(session_factory, ScriptedClient()),
        )

        assert sorted(seen) == [1, 2]
        assert result.summary == "重试失败小说: 成功 2/2"

    async def test_a_per_item_failure_is_counted_and_listed(
        self, session_factory, monkeypatch,
    ):
        async def _fake_fetch(args, ctx):            # noqa: ARG001
            if args.id == 2:
                return TaskResult(summary="failed", failed_ids=[2])
            return TaskResult(summary="ok", new_novel_titles=["ok"])

        monkeypatch.setattr(novels_mod, "novel_fetch", _fake_fetch)
        result = await failed_retry(
            FailedRetryArgs(novel_ids=[1, 2, 3]),
            make_ctx(session_factory, ScriptedClient()),
        )

        assert result.failed_ids == [2]
        assert result.summary == "重试失败小说: 成功 2/3"
        assert result.new_novel_titles == ["ok", "ok"]

    async def test_a_raising_child_counts_as_a_failure_not_a_crash(
        self, session_factory, monkeypatch,
    ):
        """``return_exceptions=True`` keeps one bad id from failing the batch."""
        async def _fake_fetch(args, ctx):            # noqa: ARG001
            if args.id == 2:
                raise RuntimeError("webview 挂了")
            return TaskResult(summary="ok")

        monkeypatch.setattr(novels_mod, "novel_fetch", _fake_fetch)
        result = await failed_retry(
            FailedRetryArgs(novel_ids=[1, 2]),
            make_ctx(session_factory, ScriptedClient()),
        )

        assert result.failed_ids == [2]
        assert result.summary == "重试失败小说: 成功 1/2"

    async def test_a_non_taskresult_child_is_a_failure(
        self, session_factory, monkeypatch,
    ):
        """``None`` is not a success: it is a child that broke its contract."""
        async def _fake_fetch(args, ctx):            # noqa: ARG001
            return None

        monkeypatch.setattr(novels_mod, "novel_fetch", _fake_fetch)
        result = await failed_retry(
            FailedRetryArgs(novel_ids=[4]), make_ctx(session_factory, ScriptedClient()),
        )

        assert result.failed_ids == [4]

    async def test_children_get_their_own_unit_of_work(self, session_factory,
                                                       monkeypatch):
        """Sessions are never shared across the gathered coroutines."""
        uows: list[object] = []

        async def _fake_fetch(args, ctx):            # noqa: ARG001
            uows.append(ctx.uow)
            return TaskResult(summary="ok")

        monkeypatch.setattr(novels_mod, "novel_fetch", _fake_fetch)
        ctx = make_ctx(session_factory, ScriptedClient())
        await failed_retry(FailedRetryArgs(novel_ids=[1, 2, 3]), ctx)

        assert len(uows) == 3
        assert len({id(u) for u in uows}) == 3
        assert all(u is not ctx.uow for u in uows)


# ---------------------------------------------------------------------------
# Argument contracts (the JSON schema the UI generates its form from)
# ---------------------------------------------------------------------------


class TestArgsContracts:
    def test_novel_fetch_defaults_to_redownload(self):
        assert NovelFetchArgs(id=1).redownload is True

    def test_ranking_defaults_are_the_daily_r18_board(self):
        args = NovelRankingArgs()
        assert (args.mode, args.days, args.force) == ("day_r18", 3, False)

    def test_search_defaults_match_the_recipe_the_ui_ships(self):
        args = NovelSearchArgs()
        assert (args.keyword, args.months, args.minlike) == ("R-18", 1, 500)

    def test_a_non_integer_id_is_rejected_before_any_network(self):
        with pytest.raises(PydanticValidationError):
            NovelFetchArgs(id="not-a-number")

    def test_failed_retry_requires_an_id_list(self):
        with pytest.raises(PydanticValidationError):
            FailedRetryArgs()
