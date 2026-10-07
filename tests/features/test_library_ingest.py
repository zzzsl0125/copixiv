"""「我的上传」入库管线测试 —— 单测 + 行为不变量。

权威：``docs/LIBRARY_DESIGN.md`` §4（上传管线）与 §7（不变量）。

单测覆盖编码探测（UTF-8 / UTF-8 BOM / UTF-16 BOM / GB18030 与 Big5 的评分
择一 / 乱码宁可报错 / 可疑占比阈值两侧）、规范化、指纹、文件名解析各形态；
行为测试钉住落库列值（``source`` / ``source_key`` / ``has_epub=3`` /
``author_name`` 非 NULL / ``path`` 在 ``download/local/`` / ``text`` /
``like=view=0``）、失败条目不写任何库表（尤其 ``failed_novel``）、同一份正文
二次上传被拒、id 是**负的时间坐标**（§3.1：坐标 = 水位 + 1、严格递增、永不复用）、
**预设作者**（整批共用：新书覆盖文件名作者、解析失败写 NULL、重复件也应用、
已挂同一作者时不写）、以及**本地行不被 pixiv 刷新路径碰**（§4 硬边界；AST 守卫在
``tests/architecture/test_library_isolation.py``）。

"不写"这类断言在值级看不出来（写回同样的值，库里的值不会有任何区别），所以
``TestPresetAuthor`` 里用 ``before_cursor_execute`` 把真正发出去的 SQL 记下来
数一数——与 ``tests/features/test_batch_metadata_repo.py`` 同一手法。
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, select, text

from copixiv.core.draft import NovelDraft
from copixiv.core.models import EpubStatus
from copixiv.db.models import Author, FailedNovel, Novel
from copixiv.features.library import ingest
from copixiv.features.library.ingest import (
    DECODE_ERROR,
    EMPTY_ERROR,
    MAX_SUSPICIOUS_RATIO,
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    UNKNOWN_AUTHOR,
    TextDecodeError,
    body_fingerprint,
    decode_txt,
    ingest_uploads,
    is_suspicious_char,
    normalize_text,
    parse_filename,
    suspicious_ratio,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository

# 所有候选编码都失败的字节流：UTF-8 / GB18030 / Big5 结构性非法，UTF-16 缺 BOM。
UNDECODABLE = b"\xff\x00\xff\x00\xff"

# 真 · 繁体（Big5）与真 · 简体（GB18030）正文样本。
BIG5_TEXT = "我的書測試：繁體中文正文，第一行。\n第二行，還有標點符號！"
GB18030_TEXT = "我的书测试：简体中文正文，第一行。\n第二行，还有标点符号！"


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


def _ingest(session_factory, download_dir: Path, files, **preset) -> list:
    """跑一遍上传管线（一个事务）并返回逐条结果。

    ``**preset`` 原样透传给 ``ingest_uploads`` 的 ``preset_author_id`` /
    ``preset_author_name``（默认不带 = 旧行为）。
    """
    with session_factory() as session:
        results = ingest_uploads(session, str(download_dir), files, **preset)
        session.commit()
    return results


def _row(session_factory, novel_id: int) -> Novel | None:
    with session_factory() as session:
        return session.get(Novel, novel_id)


def _author(session_factory, author_id: int) -> Author | None:
    with session_factory() as session:
        return session.get(Author, author_id)


@contextmanager
def _captured(engine):
    """记录这一段里真正发出去的 SQL（engine 级事件；用完即摘）。

    "重复件已挂同一作者时不写"这种断言在值级看不出来：写回同样的值，库里的值
    不会有任何区别。所以把语句抓下来——与
    ``tests/features/test_batch_metadata_repo.py::_captured`` 同一手法（那边
    还有一条提醒：后台 count 重算线程会往同一个 engine 发 SELECT，所以断言一律
    先按"是不是我关心的那个形状"筛，而不是要求窗口里只有一条语句）。
    """
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _updates(statements: list[str]) -> list[str]:
    """窗口里发出去的 UPDATE 语句（筛掉 SELECT / INSERT / 咨询锁）。"""
    return [s for s in statements if s.lstrip().upper().startswith("UPDATE")]


def _all_local_ids(session_factory) -> list[int]:
    """本地 id 按**时间轴**（``abs(id)`` 升序 = 上传先后）列出。"""
    with session_factory() as session:
        return list(session.execute(
            select(Novel.id).where(Novel.source == "local")
            .order_by(func.abs(Novel.id))
        ).scalars())


def _reset_coord_sequence(session_factory, value: int = 1) -> None:
    """把坐标游标拨回 *value*（下一次 ``nextval`` = value）。

    游标是**跨测试共享**的（生产语义：只向前、永不复用），所以想让发号有确定性
    起点的测试必须自己拨表。注意发号取 ``max(游标, 水位+1)``，所以拨到很小的值
    之后，结果由测试自己种下的 pixiv 水位决定。
    """
    with session_factory() as session:
        session.execute(
            text("SELECT setval('novel_local_coord_seq', :v, false)"), {"v": value}
        )
        session.commit()


def _seed_pixiv_watermark(session_factory, novel_id: int) -> int:
    """种一本 pixiv 书（只需要它的号把水位抬到 *novel_id*），返回该号。"""
    with session_factory() as session:
        session.add(Novel(
            id=novel_id, title=f"pixiv 书{novel_id}",
            author_name=f"作者{novel_id}", path=f"download/0000/{novel_id}.txt",
            text=5000, source="pixiv",
        ))
        session.commit()
    return novel_id


# ---------------------------------------------------------------------------
# 1. 解码（契约 §4-1）
# ---------------------------------------------------------------------------


class TestDecode:
    def test_utf8_with_bom(self):
        assert decode_txt(b"\xef\xbb\xbf" + "我的书".encode("utf-8")) == "我的书"

    def test_utf8_plain(self):
        assert decode_txt("中文正文".encode("utf-8")) == "中文正文"

    def test_ascii(self):
        assert decode_txt(b"plain ascii text\n") == "plain ascii text\n"

    def test_gb18030(self):
        assert decode_txt("中文正文测试".encode("gb18030")) == "中文正文测试"

    def test_utf16_with_bom(self):
        assert decode_txt("中文正文测试".encode("utf-16")) == "中文正文测试"

    def test_utf16_is_bom_gated(self):
        """CPython 的 ``utf-16`` 无 BOM 时**不报错**，按本机字节序硬解
        （反证：``b"ab".decode("utf-16") == "扡"``）。所以门控必须自己写 ——
        否则偶数长度的二进制流会在评分之前被静默吃成乱码。"""
        assert b"ab".decode("utf-16") == "扡"
        # 偶数长度、无 BOM、两个评分解码器都拒 → 必须报错而不是 UTF-16 乱码
        with pytest.raises(TextDecodeError):
            decode_txt(b"\x80\x81" * 8)

    def test_all_encodings_fail(self):
        with pytest.raises(TextDecodeError) as excinfo:
            decode_txt(UNDECODABLE)
        assert str(excinfo.value) == DECODE_ERROR

    # -- 评分择一：Big5 不再被 GB18030 静默抢走 ------------------------------

    def test_big5_traditional_decodes_exactly(self):
        """Big5 正文逐字解出 —— 不能被 GB18030 解成乱码（静默数据损坏）。"""
        data = BIG5_TEXT.encode("big5")

        # 分支确实被触发：GB18030 也能"成功"解这些字节，但满是私有区乱码
        garbled = data.decode("gb18030")
        assert garbled != BIG5_TEXT
        assert suspicious_ratio(garbled) > MAX_SUSPICIOUS_RATIO

        assert decode_txt(data) == BIG5_TEXT

    def test_gb18030_simplified_is_not_stolen_by_big5(self):
        """真 GB18030：两个解码器都零可疑字符 → 并列取先者（gb18030）。"""
        data = GB18030_TEXT.encode("gb18030")

        # Big5 解得出「像中文的」错字（占比 0），所以并列规则必须站在 gb18030
        assert suspicious_ratio(data.decode("big5")) == 0.0

        assert decode_txt(data) == GB18030_TEXT

    def test_implausible_bytes_are_rejected_not_mojibake(self):
        """解不出可信文本就报错（宁可 failed，也不静默导入乱码）。"""
        with pytest.raises(TextDecodeError):
            decode_txt(UNDECODABLE)
        # 随机二进制（结构性非法，两个解码器都拒）
        with pytest.raises(TextDecodeError):
            decode_txt(bytes(range(0xF5, 0xFF)) * 8)

    def test_candidate_above_threshold_is_rejected(self, monkeypatch):
        """最佳候选的可疑占比仍 > 2% → 判失败（钉住阈值本身）。"""
        monkeypatch.setattr(ingest, "_SCORED_ENCODINGS", ("gb18030",))
        # 3 / 13 ≈ 23% 私有区字符
        data = ("阿" * 10 + "\ue000" * 3).encode("gb18030")
        with pytest.raises(TextDecodeError) as excinfo:
            decode_txt(data)
        assert str(excinfo.value) == DECODE_ERROR


# ---------------------------------------------------------------------------
# 可疑字符占比：阈值两侧的边界行为（构造样本，不依赖真实文件）
# ---------------------------------------------------------------------------


class TestSuspiciousRatio:
    def test_empty_text_is_zero(self):
        assert suspicious_ratio("") == 0.0

    def test_suspicious_categories(self):
        assert is_suspicious_char("\ue000")      # Co 私有区
        assert is_suspicious_char("\ufffd")      # 替换符（类别其实是 So）
        assert is_suspicious_char("\x00")        # Cc 控制
        assert is_suspicious_char("\u200b")      # Cf 格式（零宽空格）
        assert not is_suspicious_char("中")
        assert not is_suspicious_char("A")
        for benign in ("\t", "\n", "\r"):
            assert not is_suspicious_char(benign), "换行/制表符不算可疑"

    def test_ratio_math(self):
        assert suspicious_ratio("阿" * 98 + "\ue000" * 2) == pytest.approx(0.02)
        assert suspicious_ratio("阿" * 97 + "\ue000" * 3) == pytest.approx(0.03)
        assert suspicious_ratio("正文\t第一行\n第二行\r") == 0.0

    def test_exactly_at_threshold_is_accepted(self, monkeypatch):
        """占比恰好 == 阈值不算超（判据是 ``> MAX_SUSPICIOUS_RATIO``）。"""
        monkeypatch.setattr(ingest, "_SCORED_ENCODINGS", ("gb18030",))
        sample = "阿" * 98 + "\ue000" * 2
        assert suspicious_ratio(sample) == pytest.approx(MAX_SUSPICIOUS_RATIO)
        assert decode_txt(sample.encode("gb18030")) == sample

    def test_just_above_threshold_is_rejected(self, monkeypatch):
        monkeypatch.setattr(ingest, "_SCORED_ENCODINGS", ("gb18030",))
        sample = "阿" * 97 + "\ue000" * 3
        assert suspicious_ratio(sample) > MAX_SUSPICIOUS_RATIO
        with pytest.raises(TextDecodeError):
            decode_txt(sample.encode("gb18030"))


# ---------------------------------------------------------------------------
# 2/3. 规范化与指纹（契约 §4-2、§4-3）
# ---------------------------------------------------------------------------


class TestNormalizeAndFingerprint:
    def test_crlf_becomes_lf_and_edges_stripped(self):
        assert normalize_text("\r\n\r\n第一行\r\n第二行\r\n\r\n") == "第一行\n第二行"

    def test_fingerprint_is_sha256_of_normalized_body(self):
        body = "第一行\n第二行"
        assert body_fingerprint(body) == hashlib.sha256(
            body.encode("utf-8")
        ).hexdigest()

    def test_fingerprint_stable_across_crlf_and_lf_input(self):
        assert body_fingerprint(normalize_text("甲\r\n乙")) == body_fingerprint(
            normalize_text("甲\n乙")
        )


# ---------------------------------------------------------------------------
# 5. 文件名解析（契约 §4-5）
# ---------------------------------------------------------------------------


class TestParseFilename:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("【作者】标题(完结).txt", ("标题", "作者")),
            ("【作者】标题.txt", ("标题", "作者")),
            ("[作者] 标题.txt", ("标题", "作者")),
            ("作者 - 标题.txt", ("标题", "作者")),
            ("作者 － 标题.txt", ("标题", "作者")),
            ("纯标题.txt", ("纯标题", UNKNOWN_AUTHOR)),
            ("标题（完结）.txt", ("标题", UNKNOWN_AUTHOR)),
            ("标题[全本].txt", ("标题", UNKNOWN_AUTHOR)),
            # 半角连字符不带空白不当分隔符 —— 否则标题被误拆成作者
            ("魔法少女-小圆.txt", ("魔法少女-小圆", UNKNOWN_AUTHOR)),
            # 无扩展名照旧解析
            ("没有扩展名", ("没有扩展名", UNKNOWN_AUTHOR)),
            # 客户端可能带上目录部分
            ("C:\\books\\【作者】标题.txt", ("标题", "作者")),
        ],
    )
    def test_forms(self, filename, expected):
        assert parse_filename(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        ["纯标题.txt", "...txt", "【】.txt", " - .txt", "（完结）.txt"],
    )
    def test_author_is_never_empty(self, filename):
        """§3.3：``author_name`` 必须非 NULL，解析不出即「未知作者」。"""
        _, author = parse_filename(filename)
        assert author.strip() != ""


# ---------------------------------------------------------------------------
# 6. 落库（契约 §3/§4-6）
# ---------------------------------------------------------------------------


class TestIngestCreatesLocalRow:
    def test_row_carries_the_contract_values(self, session_factory, tmp_path):
        download = tmp_path / "download"
        body = "第一行\r\n第二行"

        results = _ingest(
            session_factory, download, [("我的书.txt", body.encode("utf-8"))],
        )

        assert len(results) == 1
        result = results[0]
        assert result.status == STATUS_CREATED
        assert result.novel_id < 0
        assert result.title == "我的书"
        assert result.author_name == UNKNOWN_AUTHOR
        assert result.text == len("第一行\n第二行")

        row = _row(session_factory, result.novel_id)
        assert row is not None
        assert row.source == "local"
        assert row.source_key == body_fingerprint("第一行\n第二行")
        # §3.3 三个写死的值
        assert row.has_epub == EpubStatus.NO_IMAGES == 3
        assert row.author_name == UNKNOWN_AUTHOR
        assert row.create_time is not None
        # 其余列
        assert row.author_id is None and row.series_id is None
        assert row.like == 0 and row.view == 0
        assert row.text == len("第一行\n第二行")
        assert row.tags == []
        # 文件落在 download/local/ 下，且是规范化后的正文
        path = Path(row.path)
        assert path.parent == download / "local"
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == "第一行\n第二行"
        assert not list(path.parent.glob("*.tmp"))

    def test_id_is_the_negated_watermark_plus_one(self, session_factory, tmp_path):
        """§3.1：坐标 = 水位 + 1（水位 = 已抓取的最新 pixiv 作品号）→ id = -(水位+1)。

        上限即语义：本地书落在「已知最新作品」之上，抓取追上来后新作品自然排到
        它前面。
        """
        _reset_coord_sequence(session_factory)
        watermark = _seed_pixiv_watermark(session_factory, 2_000_000)

        result = _ingest(
            session_factory, tmp_path / "download", [("第一本.txt", "正文".encode())],
        )[0]

        assert result.novel_id == -(watermark + 1)

    def test_coordinates_grow_with_time(self, session_factory, tmp_path):
        """§3.1：id = -(坐标)，坐标越新越大 → **id 越新越小**（更负）。

        「时间」按钮（``order_by='id'``）靠服务端 ``ORDER BY abs(id)`` 落位，
        所以这里连排序一起钉：最新上传必须排最前。
        """
        _reset_coord_sequence(session_factory)
        _seed_pixiv_watermark(session_factory, 1_000)
        download = tmp_path / "download"
        first = _ingest(session_factory, download, [("a.txt", "甲".encode())])[0]
        second = _ingest(session_factory, download, [("b.txt", "乙".encode())])[0]

        assert second.novel_id < first.novel_id < 0
        assert abs(second.novel_id) == abs(first.novel_id) + 1

        with session_factory() as session:
            ordered = list(session.execute(
                select(Novel.id).where(Novel.source == "local")
                .order_by(func.abs(Novel.id).desc())
            ).scalars())
        assert ordered == [second.novel_id, first.novel_id]

    def test_batch_upload_gets_distinct_coordinates(self, session_factory, tmp_path):
        """一次上传多个文件时水位不变 —— 发号必须有单调保底，否则第 2 个文件
        就会拿同一个坐标、撞主键（``id = -水位`` 的朴素写法正是这样死的）。
        """
        _reset_coord_sequence(session_factory)
        watermark = _seed_pixiv_watermark(session_factory, 3_000_000)

        results = _ingest(session_factory, tmp_path / "download", [
            ("甲.txt", "甲正文".encode()),
            ("乙.txt", "乙正文".encode()),
            ("丙.txt", "丙正文".encode()),
        ])

        assert [r.status for r in results] == [STATUS_CREATED] * 3
        ids = [r.novel_id for r in results]
        assert ids == [-(watermark + 1), -(watermark + 2), -(watermark + 3)]
        assert len(set(ids)) == 3

    def test_empty_pixiv_axis_uses_the_time_seed(
        self, session_factory, tmp_path, monkeypatch,
    ):
        """库中一行 pixiv 都没有时按时间线性外推（否则本地书会被钉在 id 轴起点，
        等第一次抓取落地就永远沉底）。"""
        from copixiv.features.library import local_axis

        _reset_coord_sequence(session_factory)
        monkeypatch.setattr(local_axis, "seed_coord", lambda now=None: 12_345)

        result = _ingest(
            session_factory, tmp_path / "download", [("第一本.txt", "正文".encode())],
        )[0]

        assert result.novel_id == -12_345

    def test_deleted_id_is_never_reused(self, session_factory, tmp_path):
        """id 用户可见（DSL ``id:`` / 导出 ``{id}``）—— 删掉最新那本后，
        新书必须拿**更大**的坐标（游标只向前，而非 ``max(坐标)`` 续号）。"""
        _reset_coord_sequence(session_factory)
        _seed_pixiv_watermark(session_factory, 1_000)
        download = tmp_path / "download"
        first = _ingest(session_factory, download, [("甲.txt", "甲正文".encode())])[0]
        second = _ingest(session_factory, download, [("乙.txt", "乙正文".encode())])[0]
        with session_factory() as session:
            session.execute(delete(Novel).where(Novel.id == second.novel_id))
            session.commit()

        third = _ingest(session_factory, download, [("丙.txt", "丙正文".encode())])[0]

        assert abs(third.novel_id) > abs(second.novel_id) > abs(first.novel_id)
        assert third.novel_id < second.novel_id < first.novel_id
        assert _all_local_ids(session_factory) == [first.novel_id, third.novel_id]

    def test_mixed_batch_is_partial_success(self, session_factory, tmp_path):
        download = tmp_path / "download"
        results = _ingest(session_factory, download, [
            ("好的.txt", "正文".encode("utf-8")),
            ("坏的.txt", UNDECODABLE),
            ("空的.txt", b"   \r\n  "),
        ])

        assert [r.status for r in results] == [
            STATUS_CREATED, STATUS_FAILED, STATUS_FAILED,
        ]
        assert results[1].error == DECODE_ERROR
        assert results[2].error == EMPTY_ERROR
        # 失败项不写库
        with session_factory() as session:
            rows = list(session.execute(select(Novel)).scalars())
        assert [r.id for r in rows] == [results[0].novel_id]

    def test_big5_body_is_stored_verbatim(self, session_factory, tmp_path):
        """Big5 上传端到端：磁盘上的正文与原文逐字相等（不是乱码）。"""
        results = _ingest(
            session_factory, tmp_path / "download",
            [("繁體書.txt", BIG5_TEXT.encode("big5"))],
        )

        assert results[0].status == STATUS_CREATED
        row = _row(session_factory, results[0].novel_id)
        assert Path(row.path).read_text(encoding="utf-8") == BIG5_TEXT
        assert row.text == len(BIG5_TEXT)
        assert row.source_key == body_fingerprint(BIG5_TEXT)


# ---------------------------------------------------------------------------
# §7-2：失败永不写 failed_novel
# ---------------------------------------------------------------------------


class TestFailuresDoNotWriteTables:
    def test_undecodable_upload_writes_no_table(self, session_factory, tmp_path):
        download = tmp_path / "download"
        results = _ingest(session_factory, download, [("坏.txt", UNDECODABLE)])

        assert results[0].status == STATUS_FAILED
        assert results[0].novel_id is None
        with session_factory() as session:
            assert session.execute(
                select(Novel).where(Novel.source == "local")
            ).first() is None
            assert session.execute(select(FailedNovel)).first() is None

    def test_failed_item_never_reaches_the_failure_ledger(
        self, session_factory, tmp_path,
    ):
        """批量里的失败项也不进 ``failed_novel``（§4-7：failed_retry 会拿它去请求 Pixiv）。"""
        download = tmp_path / "download"
        _ingest(session_factory, download, [
            ("好.txt", "正文".encode()),
            ("坏.txt", UNDECODABLE),
            ("空.txt", b""),
        ])
        with session_factory() as session:
            assert list(session.execute(select(FailedNovel)).scalars()) == []


# ---------------------------------------------------------------------------
# §7-7：同一份正文二次上传被拒
# ---------------------------------------------------------------------------


class TestDuplicate:
    def test_second_upload_is_duplicate(self, session_factory, tmp_path):
        download = tmp_path / "download"
        first = _ingest(
            session_factory, download, [("我的书.txt", "同一份正文".encode())],
        )[0]

        second = _ingest(
            session_factory, download, [("副本.txt", "同一份正文".encode())],
        )[0]

        assert second.status == STATUS_DUPLICATE
        assert second.novel_id == first.novel_id
        assert second.title == first.title
        with session_factory() as session:
            assert len(list(session.execute(
                select(Novel).where(Novel.source == "local")
            ).scalars())) == 1

    def test_duplicate_inside_one_batch(self, session_factory, tmp_path):
        download = tmp_path / "download"
        results = _ingest(session_factory, download, [
            ("a.txt", "同一份正文".encode()),
            ("b.txt", "同一份正文".encode()),
        ])
        assert [r.status for r in results] == [STATUS_CREATED, STATUS_DUPLICATE]
        assert results[0].novel_id == results[1].novel_id

    def test_normalization_equal_bodies_collide(self, session_factory, tmp_path):
        """指纹算在规范化正文上：``\\r\\n`` 与 ``\\n`` 是同一份正文。"""
        download = tmp_path / "download"
        first = _ingest(session_factory, download, [("a.txt", "甲\r\n乙".encode())])[0]
        second = _ingest(session_factory, download, [("b.txt", "甲\n乙".encode())])[0]
        assert second.status == STATUS_DUPLICATE
        assert second.novel_id == first.novel_id


# ---------------------------------------------------------------------------
# 预设作者（``preset_author_id`` / ``preset_author_name``）—— 整批共用的作者
# ---------------------------------------------------------------------------

PRESET_AUTHOR_ID = 555_001
OTHER_NOVEL_ID = 4_242          # 同一位作者名下的另一本书（规范化要一起命中）


def _seed_author(session_factory, author_id: int, name: str | None = None) -> None:
    """种一行 ``author``（预设作者的外键目标）。

    API 侧由读事务的 ``ensure_exists`` 保证它在写事务之前就位；直连管线的调用方
    自己负责这一步——``novel.author_id`` 有外键，没有它这一条会以
    "入库冲突"失败。
    """
    with session_factory() as session:
        session.add(Author(author_id=author_id, author_name=name))
        session.commit()


class TestPresetAuthor:
    def test_new_book_writes_the_preset_author_over_the_filename(
        self, session_factory, tmp_path,
    ):
        """新书：id 与名字都按预设写，文件名里解析出的作者名被覆盖。"""
        _seed_author(session_factory, PRESET_AUTHOR_ID)

        results = _ingest(
            session_factory, tmp_path / "download",
            [("【文件名作者】我的书.txt", "正文".encode())],
            preset_author_id=PRESET_AUTHOR_ID, preset_author_name="预设作者",
        )

        result = results[0]
        assert result.status == STATUS_CREATED
        assert result.author_name == "预设作者"
        row = _row(session_factory, result.novel_id)
        assert row.author_id == PRESET_AUTHOR_ID
        assert row.author_name == "预设作者"
        # 行是真入库了（不是只在响应里改了口径）
        assert Path(row.path).read_text(encoding="utf-8") == "正文"
        # 作者行也被收尾写上名字（update_author_name 两个都写）
        assert _author(session_factory, PRESET_AUTHOR_ID).author_name == "预设作者"

    def test_unresolved_name_is_stored_as_null(
        self, session_factory, tmp_path, pg_engine,
    ):
        """解析失败 → ``author_name`` **显式**写 NULL 覆盖文件名解析值，且不写作者行。

        「id 是身份、名字是缓存」：退回文件名里那个名字会让这张卡显示"id 指向
        A、名字写着 B"；写「未知作者」则是拿一个看起来确定的假名盖住"待解析"。
        NULL 是仓库**认可**的中间态——不是"有兜底任务"（``sync_empty_name``
        默认禁用）。
        """
        _seed_author(session_factory, PRESET_AUTHOR_ID)

        with _captured(pg_engine) as statements:
            results = _ingest(
                session_factory, tmp_path / "download",
                [("【文件名作者】我的书.txt", "正文".encode())],
                preset_author_id=PRESET_AUTHOR_ID, preset_author_name=None,
            )

        row = _row(session_factory, results[0].novel_id)
        assert row.author_id == PRESET_AUTHOR_ID
        assert row.author_name is None
        assert _author(session_factory, PRESET_AUTHOR_ID).author_name is None
        # 解析不出名字就不调用 update_author_name（语句级证据：一条 UPDATE 都没有）
        assert _updates(statements) == []

    def test_duplicate_applies_the_preset_author(self, session_factory, tmp_path):
        """重复件也应用（用户拍板）：改这一行 + 规范化作者行与同作者的其它行。"""
        download = tmp_path / "download"
        first = _ingest(
            session_factory, download, [("【旧作者】我的书.txt", "同一份正文".encode())],
        )[0]
        # 同一位作者名下的另一本 pixiv 书，名字是旧的：规范化要把它一起改掉
        _seed_author(session_factory, PRESET_AUTHOR_ID, "旧名字")
        with session_factory() as session:
            session.add(Novel(
                id=OTHER_NOVEL_ID, title="同作者的 pixiv 书",
                author_id=PRESET_AUTHOR_ID, author_name="旧名字",
                path=f"download/0000/{OTHER_NOVEL_ID}.txt", text=100, source="pixiv",
            ))
            session.commit()

        second = _ingest(
            session_factory, download, [("副本.txt", "同一份正文".encode())],
            preset_author_id=PRESET_AUTHOR_ID, preset_author_name="预设作者",
        )[0]

        assert second.status == STATUS_DUPLICATE
        assert second.novel_id == first.novel_id      # 既有语义不变
        assert second.title == first.title
        assert second.author_name == "预设作者"        # 回显这次写进去的名字
        row = _row(session_factory, first.novel_id)
        assert row.author_id == PRESET_AUTHOR_ID
        assert row.author_name == "预设作者"
        # 作者行 + 同作者的全部 novel 行一起规范化（``IS DISTINCT FROM``）
        assert _author(session_factory, PRESET_AUTHOR_ID).author_name == "预设作者"
        assert _row(session_factory, OTHER_NOVEL_ID).author_name == "预设作者"
        # 重复件不建新行
        assert _all_local_ids(session_factory) == [first.novel_id]

    def test_duplicate_already_on_the_same_author_is_not_written(
        self, session_factory, tmp_path, pg_engine,
    ):
        """已挂同一作者、名字也一样 → 一个字节都不写（重复件尽量不动库）。"""
        download = tmp_path / "download"
        _seed_author(session_factory, PRESET_AUTHOR_ID)
        first = _ingest(
            session_factory, download, [("我的书.txt", "同一份正文".encode())],
            preset_author_id=PRESET_AUTHOR_ID, preset_author_name="预设作者",
        )[0]

        with _captured(pg_engine) as statements:
            second = _ingest(
                session_factory, download, [("副本.txt", "同一份正文".encode())],
                preset_author_id=PRESET_AUTHOR_ID, preset_author_name="预设作者",
            )[0]

        assert second.status == STATUS_DUPLICATE
        assert second.author_name == "预设作者"        # 回显照旧（值本来就是这个）
        row = _row(session_factory, first.novel_id)
        assert (row.author_id, row.author_name) == (PRESET_AUTHOR_ID, "预设作者")
        assert _updates(statements) == []

    def test_without_a_preset_a_duplicate_changes_nothing(
        self, session_factory, tmp_path, pg_engine,
    ):
        """没给 ``preset_author_id`` → 与旧行为逐字一致：不改库、也不回显作者名。"""
        download = tmp_path / "download"
        first = _ingest(
            session_factory, download,
            [("【文件名作者】我的书.txt", "同一份正文".encode())],
        )[0]

        with _captured(pg_engine) as statements:
            second = _ingest(
                session_factory, download, [("副本.txt", "同一份正文".encode())],
            )[0]

        assert second.status == STATUS_DUPLICATE
        assert second.novel_id == first.novel_id
        assert second.title == first.title
        assert second.author_name is None             # 旧行为：duplicate 不带作者名
        row = _row(session_factory, first.novel_id)
        assert row.author_id is None and row.author_name == "文件名作者"
        assert _updates(statements) == []

    def test_the_writeback_runs_once_per_batch(
        self, session_factory, tmp_path, pg_engine,
    ):
        """一个 id 写 N 本：作者行收尾**一次**，不是每条一次。

        ``update_author_name`` 的第一条语句是"该作者全部 novel 行"范围的
        set-based UPDATE——放进循环里就是 N 次全库范围 UPDATE。
        """
        _seed_author(session_factory, PRESET_AUTHOR_ID)

        with _captured(pg_engine) as statements:
            results = _ingest(
                session_factory, tmp_path / "download",
                [
                    ("甲.txt", "甲正文".encode()),
                    ("乙.txt", "乙正文".encode()),
                    ("丙.txt", "丙正文".encode()),
                ],
                preset_author_id=PRESET_AUTHOR_ID, preset_author_name="预设作者",
            )

        assert [r.author_name for r in results] == ["预设作者"] * 3
        assert len(_all_local_ids(session_factory)) == 3
        updates = _updates(statements)
        # 恰好两条：规范化同作者 novel 行 + 写 author 行（各一次）
        assert len(updates) == 2
        heads = [s.lstrip().upper() for s in updates]
        assert sum(1 for s in heads if s.startswith("UPDATE AUTHOR")) == 1
        assert sum(1 for s in heads if s.startswith("UPDATE NOVEL")) == 1

    def test_a_preset_name_without_an_id_is_ignored(self, session_factory, tmp_path):
        """只给名字不给 id = 没给预设（名字是 id 的派生值，不能单独写）。"""
        results = _ingest(
            session_factory, tmp_path / "download",
            [("【文件名作者】我的书.txt", "正文".encode())],
            preset_author_name="预设作者",
        )

        row = _row(session_factory, results[0].novel_id)
        assert row.author_id is None
        assert row.author_name == "文件名作者"


# ---------------------------------------------------------------------------
# §4 硬边界：本地行永不进 pixiv 刷新路径（行为级；AST 守卫另见 architecture）
# ---------------------------------------------------------------------------

async def test_local_row_untouched_by_pixiv_upsert(session_factory, tmp_path):
    download = tmp_path / "download"
    local = _ingest(
        session_factory, download, [("本地书.txt", "本地正文".encode())],
    )[0]
    before = _row(session_factory, local.novel_id)

    with session_factory() as session:
        await SQLAlchemyNovelRepository(session).upsert_novels([
            NovelDraft(
                id=12345, title="pixiv 书", author_id=777,
                path="download/0000/pixiv.txt",
            ),
        ])
        session.commit()

    after = _row(session_factory, local.novel_id)
    assert after is not None
    assert after.source == "local"
    assert after.source_key == before.source_key
    assert after.title == before.title
    assert after.path == before.path
    assert after.has_epub == before.has_epub
    assert after.author_name == before.author_name
    # pixiv 行照常写入，且默认 source='pixiv'
    assert _row(session_factory, 12345).source == "pixiv"


# ---------------------------------------------------------------------------
# id 分配并发安全（§3.1：锚点 + 递增不能撞车）
# ---------------------------------------------------------------------------


class TestConcurrentUploads:
    """两个连接同时上传 —— 事务级咨询锁让「分配 id + 指纹去重」串行。"""

    @staticmethod
    def _upload_concurrently(session_factory, download, first, second):
        barrier = threading.Barrier(2)
        results: dict[str, list] = {}

        def worker(label: str, name: str, body: bytes) -> None:
            barrier.wait(timeout=30)
            with session_factory() as session:
                outcome = ingest_uploads(
                    session, str(download), [(name, body)],
                )
                session.commit()
            results[label] = outcome

        threads = [
            threading.Thread(target=worker, args=("a", *first)),
            threading.Thread(target=worker, args=("b", *second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(t.is_alive() for t in threads), "并发上传线程卡死"
        return results["a"][0], results["b"][0]

    def test_distinct_bodies_get_distinct_ids(self, session_factory, tmp_path):
        first, second = self._upload_concurrently(
            session_factory, tmp_path / "download",
            ("甲.txt", "正文甲".encode("utf-8")),
            ("乙.txt", "正文乙".encode("utf-8")),
        )

        assert first.status == second.status == STATUS_CREATED
        assert first.novel_id != second.novel_id
        assert len(_all_local_ids(session_factory)) == 2

    def test_same_body_concurrently_creates_one_row(self, session_factory, tmp_path):
        first, second = self._upload_concurrently(
            session_factory, tmp_path / "download",
            ("甲.txt", "同一份正文".encode("utf-8")),
            ("乙.txt", "同一份正文".encode("utf-8")),
        )

        created, duplicate = sorted(
            (first, second), key=lambda r: r.status,
        )
        assert created.status == STATUS_CREATED
        assert duplicate.status == STATUS_DUPLICATE
        assert duplicate.novel_id == created.novel_id
        assert len(_all_local_ids(session_factory)) == 1
