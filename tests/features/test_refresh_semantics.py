"""抓取刷新语义（2026-09 拍板）—— 四条改动的行为契约。

① ``create_time`` 退出无条件覆盖白名单 → 仅当库中为 NULL 时才补
② **正文变更检测**：刷新时读本地正文现算 sha256 与刚抓到的正文比，相同 →
   不写盘、不强制重跑资产；不同 → 覆盖正文 + 强制重跑
③ ``has_epub`` 归位：只有"这次确实重写了正文文件"才由 upsert 写它，否则不表态
④ ``tags`` 改并集：只增不减

这些测试钉的是**语义**（"用户留下的值会不会被来源顶掉"、"文件字节有没有被
重写"），不是实现细节。§2 的四条改动全部零 schema 变更，所以没有迁移测试。

对照事实：改动前的实测现状记录在 ``docs/NOVEL_EDIT_DESIGN.md`` §2.1.1。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy import text as sa_text

from copixiv.core.draft import build_from_novel_info, build_novel
from copixiv.core.models import EpubStatus
from copixiv.core.services import body_unchanged, sha256_text
from copixiv.db.models import Novel, Tag
from copixiv.features.library.ingest import body_fingerprint, normalize_text
from copixiv.features.novels.download_novel import fetch_novel_and_assets
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.storage.file_storage import FileStorage

NID = 4242
BODY_TITLE = "正文标题"
USER_TIME = datetime(2019, 1, 1, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _novel_info(**over):
    """榜单/搜索/关注流/作者目录 的 novelInfo 形状（列表路径的输入）。"""
    data = dict(
        id=NID,
        title="来源标题",
        caption="来源简介",
        create_date="2020-05-05 12:00:00",
        total_bookmarks=999,
        total_view=888,
        text_length=555,
        tags=["来源新标签"],
        user=SimpleNamespace(id=999, name="来源作者"),
        series=SimpleNamespace(id=777, title="来源系列", index=3),
    )
    data.update(over)
    return SimpleNamespace(**data)


def _seed_row(session_factory, **over):
    """一行"用户改过的" pixiv 书：所有字段都是非来源值。"""
    values = dict(
        id=NID,
        title="用户改的标题",
        author_name="用户作者",
        path=f"download/0000/用户改的标题_{NID}.txt",
        like=1, view=2, text=100,
        caption="用户简介",
        series_name="用户系列", series_index=7,
        create_time=USER_TIME,
        has_epub=EpubStatus.DONE,
        tags=["用户手加"],
        source="pixiv",
    )
    values.update(over)
    with session_factory() as s:
        s.add(Novel(**values))
        s.commit()


def _row(session_factory, nid: int = NID):
    with session_factory() as s:
        return s.get(Novel, nid)


async def _refresh_list_path(session_factory, **over):
    """走列表路径的 metadata-only upsert（榜单/搜索/关注流/作者目录）。"""
    with session_factory() as s:
        await SQLAlchemyNovelRepository(s).upsert_novels(
            [build_from_novel_info(_novel_info(**over))],
        )
        s.commit()


# ---------------------------------------------------------------------------
# ① 白名单：谁还被无条件覆盖，谁不再被覆盖
# ---------------------------------------------------------------------------

async def test_source_authoritative_fields_are_still_overwritten(
    clean_db, session_factory,
):
    """title / caption / series_name / like / view / text 照旧由来源说了算。"""
    _seed_row(session_factory)
    await _refresh_list_path(session_factory)

    row = _row(session_factory)
    assert row.title == "来源标题"
    assert row.caption == "来源简介"
    assert row.series_name == "来源系列"
    assert (row.like, row.view, row.text) == (999, 888, 555)
    # 非空者胜（本来就不在覆盖白名单里，这里是防回归的对照）
    assert row.author_name == "用户作者"
    assert row.series_index == 7


async def test_create_time_is_no_longer_overwritten(clean_db, session_factory):
    """① 的核心断言：库里已有日期 → 来源的 create_date 顶不掉它。"""
    _seed_row(session_factory)
    await _refresh_list_path(session_factory)  # create_date=2020-05-05

    assert _row(session_factory).create_time == USER_TIME


async def test_create_time_is_filled_when_null(clean_db, session_factory):
    """① 的另一半：库中为空时，来源仍然可以补上。

    比较的是**时刻**而不是本地时区字段：``2020-05-05`` 被 ``parse_pixiv_time``
    当作 Asia/Tokyo 零点，回读时按会话时区（+08）显示成 05-04 23:00。
    """
    _seed_row(session_factory, create_time=None)
    await _refresh_list_path(session_factory)

    filled = _row(session_factory).create_time
    assert filled is not None
    assert filled == datetime(2020, 5, 5, tzinfo=ZoneInfo("Asia/Tokyo"))


async def test_empty_source_create_date_cannot_null_out_a_stored_date(
    clean_db, session_factory,
):
    """① 顺带堵掉的坑：``create_date`` 缺失 → ``build_from_novel_info`` 给
    ``""`` → ``parse_pixiv_time("")`` → ``None``。改动前它在覆盖白名单里，
    会把这个 None 写进库、把已有日期抹掉。"""
    _seed_row(session_factory)
    await _refresh_list_path(session_factory, create_date="")

    assert _row(session_factory).create_time == USER_TIME


# ---------------------------------------------------------------------------
# ② 纯函数：sha256_text / body_unchanged
# ---------------------------------------------------------------------------

def test_sha256_text_matches_known_digests():
    assert sha256_text("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert sha256_text("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_text("正文") == sha256_text("正文")
    assert sha256_text("正文") != sha256_text("正文 ")


def test_body_unchanged_boundary_table():
    """任一侧为空 → False（"没有可用的本地正文" 不能算作"没变"）。"""
    assert body_unchanged(None, "正文") is False
    assert body_unchanged("正文", None) is False
    assert body_unchanged("", "正文") is False
    assert body_unchanged("正文", "") is False
    assert body_unchanged(None, None) is False
    assert body_unchanged("正文", "正文") is True
    assert body_unchanged("正文", "另一段正文") is False


def test_body_unchanged_compares_raw_text_without_normalisation():
    """变更检测比的是**写盘原文**：只差换行也算变了。

    这是与本地书去重键（``body_fingerprint``，先 normalize_text）的**有意**
    差别——两者输入不同，共用同一个哈希原语。
    """
    assert body_unchanged("a\r\nb", "a\nb") is False
    # 去重键那一侧则相反：规范化后两种换行写法必须同值
    assert body_fingerprint(normalize_text("a\r\nb")) == sha256_text("a\nb")
    assert body_fingerprint(normalize_text("a\nb ")) == sha256_text("a\nb")


# ---------------------------------------------------------------------------
# ④ tags 并集：只增不减
# ---------------------------------------------------------------------------

async def test_tags_are_merged_and_never_removed(clean_db, session_factory):
    _seed_row(session_factory, tags=["用户手加", "来源老标签"])

    await _refresh_list_path(session_factory, tags=["来源老标签", "来源新标签"])
    assert set(_row(session_factory).tags) == {"用户手加", "来源老标签", "来源新标签"}

    # 幂等：重复刷新不改变集合、不产生重复项
    await _refresh_list_path(session_factory, tags=["来源老标签", "来源新标签"])
    assert set(_row(session_factory).tags) == {"用户手加", "来源老标签", "来源新标签"}

    # 边界：来源不再送旧标签 → 库里一个都不删（只增不减）
    await _refresh_list_path(session_factory, tags=["仅来源这一轮"])
    assert set(_row(session_factory).tags) == {
        "用户手加", "来源老标签", "来源新标签", "仅来源这一轮",
    }


async def test_tag_reference_counts_stay_exact_after_merge(
    clean_db, session_factory,
):
    """并集写入后，``tag`` 行与 ``reference_count`` 仍与 novel.tags 精确一致
    （语句级触发器按净增量维护，合并方向不影响正确性）。"""
    _seed_row(session_factory, tags=["用户手加", "来源老标签"])
    await _refresh_list_path(session_factory, tags=["来源老标签", "来源新标签"])

    with session_factory() as s:
        truth = dict(s.execute(sa_text(
            "SELECT t.tag_name, count(*) FROM novel n, "
            "unnest(n.tags) AS t(tag_name) GROUP BY 1"
        )).all())
        stored = dict(s.execute(select(Tag.name, Tag.reference_count)).all())

    assert truth, "并集后库里应当有标签"
    assert {name: stored.get(name) for name in truth} == truth
    # 新标签确实建了 tag 行（触发器插入分支）
    assert "来源新标签" in stored


# ---------------------------------------------------------------------------
# ③ has_epub：只有"这次确实重写了正文"才由 upsert 写
# ---------------------------------------------------------------------------

async def test_metadata_refresh_never_touches_has_epub(clean_db, session_factory):
    """元数据型 draft 的 has_epub 是 None（被 repo 弹出）→ 状态原地不动。"""
    _seed_row(session_factory, has_epub=EpubStatus.DONE)
    await _refresh_list_path(session_factory)
    assert _row(session_factory).has_epub == EpubStatus.DONE

    # 产线（update_has_epub_status）写下的状态同样不被刷新打回
    with session_factory() as s:
        await SQLAlchemyNovelRepository(s).update_has_epub_status(
            [NID], EpubStatus.NO_IMAGES,
        )
        s.commit()
    await _refresh_list_path(session_factory, title="又改了一次标题")
    assert _row(session_factory).has_epub == EpubStatus.NO_IMAGES


async def test_real_status_in_draft_is_still_written(clean_db, session_factory):
    """③ 的边界：draft 带着真值时照写 —— 归位靠下载路径把值置 None，
    而不是靠把 ``has_epub`` 从白名单里删掉。"""
    _seed_row(session_factory, has_epub=EpubStatus.DONE)

    with session_factory() as s:
        await SQLAlchemyNovelRepository(s).upsert_novels([
            build_novel(
                id=NID, title="带图新标题", author_id=999,
                has_epub=EpubStatus.PENDING,
            ),
        ])
        s.commit()

    assert _row(session_factory).has_epub == EpubStatus.PENDING


# ---------------------------------------------------------------------------
# ② 下载路径决策：文件写不写 / 资产强不强制 / has_epub 表不表态
# ---------------------------------------------------------------------------

class _FakeClient:
    """只要能 ``await webview_novel(id)`` 返回 webview 形状的对象即可。"""

    def __init__(self, payload):
        self.payload = payload

    async def webview_novel(self, novel_id):  # noqa: ARG002 — 形状对齐
        return self.payload


class _RecordingDownloader:
    def __init__(self):
        self.calls: list[tuple[object, bool]] = []

    async def process_novel_assets(self, novel, force: bool = False):
        self.calls.append((novel, force))

    @property
    def force(self) -> bool:
        assert self.calls, "process_novel_assets 必须被调用（EPUB 缺失要能自愈）"
        return self.calls[-1][1]


def _payload(body: str, *, nid: int = NID):
    return SimpleNamespace(
        id=nid,
        title=BODY_TITLE,
        text=body,
        user_id=999,
        rating=SimpleNamespace(bookmark=1, view=2),
        caption="简介",
        series_id=None,
        series_title=None,
        series_navigation=None,
        cdate="2020-01-01",
        tags=[],
        images=None,
        illusts=None,
        cover_url=None,
    )


def _text_path(storage: FileStorage) -> "object":
    return storage.novel_text_path(NID, BODY_TITLE)


async def test_download_writes_body_when_no_local_copy(tmp_path):
    storage = FileStorage(str(tmp_path))
    downloader = _RecordingDownloader()

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload("正文A")), storage, downloader,
    )

    assert _text_path(storage).read_text(encoding="utf-8") == "正文A"
    assert downloader.force is True
    # 正文是新写下去的 → draft 的真值保留（无占位符 → 3）
    assert draft.has_epub == EpubStatus.NO_IMAGES


async def test_download_keeps_local_copy_when_body_unchanged(tmp_path):
    storage = FileStorage(str(tmp_path))
    body = "正文 [uploadedimage:12345] 尾巴"   # 带占位符，凸显"不重新判定"
    storage.save_novel_text(NID, BODY_TITLE, body)
    path = _text_path(storage)
    mtime_before = path.stat().st_mtime_ns
    downloader = _RecordingDownloader()

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload(body)), storage, downloader,
    )

    assert path.stat().st_mtime_ns == mtime_before, "正文未变 → 一个字节都不该写"
    assert path.read_text(encoding="utf-8") == body
    assert downloader.force is False, "正文未变 → 不强制重建资产"
    assert draft.has_epub is None, "没重写正文 → 不重新判定 EPUB 状态"


async def test_download_overwrites_when_body_changed(tmp_path):
    storage = FileStorage(str(tmp_path))
    storage.save_novel_text(NID, BODY_TITLE, "旧正文")
    downloader = _RecordingDownloader()

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload("新正文")), storage, downloader,
    )

    assert _text_path(storage).read_text(encoding="utf-8") == "新正文"
    assert downloader.force is True, "正文变了 → EPUB 必须重建"
    assert draft.has_epub == EpubStatus.NO_IMAGES


async def test_download_marks_placeholder_body_as_pending(tmp_path):
    """正文被重写且带图片占位符 → 状态由新正文判定（PENDING）。"""
    storage = FileStorage(str(tmp_path))
    downloader = _RecordingDownloader()

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload("正文 [pixivimage:67890]")), storage, downloader,
    )

    assert draft.has_epub == EpubStatus.PENDING
    assert downloader.force is True


async def test_redownload_ignores_the_local_copy(tmp_path):
    """``redownload=True`` = 用户显式强制：不看本地副本，照写照重建。"""
    storage = FileStorage(str(tmp_path))
    storage.save_novel_text(NID, BODY_TITLE, "正文A")
    downloader = _RecordingDownloader()

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload("正文A")), storage, downloader,
        redownload=True,
    )

    assert downloader.force is True
    assert draft.has_epub == EpubStatus.NO_IMAGES, "强制路径照旧写状态"


async def test_download_uses_stored_anchor_after_source_rename(tmp_path):
    """A source-side rename must not move the body off the reader's path.

    ``novel.path`` is written once and then only filled when NULL (a file
    anchor), while ``build_path`` recomputes from the current title.  Writing
    with the recomputed path stored the body at ``新标题_<id>.txt`` while every
    reader (download / export / delete) kept resolving ``旧标题_<id>.txt``:
    the reader got stale text, and the "did the body change?" check compared
    against a file that did not exist, so it always reported "changed".

    With the anchor passed in, the new body lands on the stored path and the
    old file is the one being updated — no orphan appears.
    """
    storage = FileStorage(str(tmp_path))
    downloader = _RecordingDownloader()

    # The row already exists under the OLD title.
    stored = storage.save_novel_text(NID, "旧标题", "旧正文")

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload("新正文")), storage, downloader,
        stored_path=str(stored),
    )

    assert draft.path == str(stored), "draft 必须采用库里的锚"
    assert stored.read_text(encoding="utf-8") == "新正文"
    # No orphan file under the new title.
    siblings = sorted(p.name for p in stored.parent.iterdir())
    assert siblings == [stored.name]


async def test_anchor_outside_download_root_is_ignored(tmp_path):
    """A polluted ``path`` must not become an arbitrary write target."""
    storage = FileStorage(str(tmp_path))
    downloader = _RecordingDownloader()

    draft = await fetch_novel_and_assets(
        NID, _FakeClient(_payload("正文")), storage, downloader,
        stored_path="/etc/not-our-file.txt",
    )

    assert draft.path != "/etc/not-our-file.txt"
    assert str(draft.path).startswith(str(tmp_path)), "回退到根内路径"
