"""「我的上传」入库管线测试 —— 单测 + 行为不变量。

权威：``docs/LIBRARY_DESIGN.md`` §4（上传管线）与 §7（不变量）。

单测覆盖编码探测（UTF-8 / UTF-8 BOM / UTF-16 BOM / GB18030 与 Big5 的评分
择一 / 乱码宁可报错 / 可疑占比阈值两侧）、规范化、指纹、文件名解析各形态；
行为测试钉住落库列值（``source`` / ``source_key`` / ``has_epub=3`` /
``author_name`` 非 NULL / ``path`` 在 ``download/local/`` / ``text`` /
``like=view=0``）、失败条目不写任何库表（尤其 ``failed_novel``）、同一份正文
二次上传被拒、id 越新越大且**永不复用**，以及**本地行不被 pixiv 刷新路径碰**
（§4 硬边界；AST 守卫在 ``tests/architecture/test_library_isolation.py``）。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text

from copixiv.core.draft import NovelDraft
from copixiv.core.models import EpubStatus
from copixiv.db.models import FailedNovel, Novel
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


def _ingest(session_factory, download_dir: Path, files) -> list:
    """跑一遍上传管线（一个事务）并返回逐条结果。"""
    with session_factory() as session:
        results = ingest_uploads(session, str(download_dir), files)
        session.commit()
    return results


def _row(session_factory, novel_id: int) -> Novel | None:
    with session_factory() as session:
        return session.get(Novel, novel_id)


def _all_local_ids(session_factory) -> list[int]:
    with session_factory() as session:
        return list(session.execute(
            select(Novel.id).where(Novel.source == "local").order_by(Novel.id)
        ).scalars())


def _reset_local_id_sequence(session_factory) -> None:
    """把序列拨回出厂状态：下一次 ``nextval`` = -(10**9) + 1。

    序列是**跨测试共享**的（生产语义：永不回退、永不复用），所以想要确定性
    起点的测试必须自己拨表。
    """
    with session_factory() as session:
        session.execute(text(
            "SELECT setval('novel_local_id_seq', -999999999, false)"
        ))
        session.commit()


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

    def test_ids_grow_with_time(self, session_factory, tmp_path):
        """§3.1：``order_by='id'`` 倒序时最新上传必须排最前 → id 越新越大。"""
        download = tmp_path / "download"
        first = _ingest(session_factory, download, [("a.txt", "甲".encode())])[0]
        second = _ingest(session_factory, download, [("b.txt", "乙".encode())])[0]

        assert first.novel_id < second.novel_id < 0
        with session_factory() as session:
            ids = list(session.execute(
                select(Novel.id).where(Novel.source == "local")
                .order_by(Novel.id.desc())
            ).scalars())
        assert ids == [second.novel_id, first.novel_id]

    def test_first_id_is_the_anchor(self, session_factory, tmp_path):
        """§3.1：``id = -(10**9) + n``，n 从 1 起 → 第一本是 -999999999。"""
        _reset_local_id_sequence(session_factory)
        result = _ingest(
            session_factory, tmp_path / "download", [("第一本.txt", "正文".encode())],
        )[0]

        assert result.novel_id == -(10 ** 9) + 1

    def test_deleted_id_is_never_reused(self, session_factory, tmp_path):
        """id 用户可见（DSL ``id:`` / 导出 ``{id}``）—— 删掉最新那本后，
        新书必须拿**更大**的 id（专用序列发号，而非 ``max(id)`` 续号）。"""
        download = tmp_path / "download"
        first = _ingest(session_factory, download, [("甲.txt", "甲正文".encode())])[0]
        second = _ingest(session_factory, download, [("乙.txt", "乙正文".encode())])[0]
        with session_factory() as session:
            session.execute(delete(Novel).where(Novel.id == second.novel_id))
            session.commit()

        third = _ingest(session_factory, download, [("丙.txt", "丙正文".encode())])[0]

        assert third.novel_id > second.novel_id > first.novel_id
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
