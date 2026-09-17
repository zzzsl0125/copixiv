"""本地 TXT 入库管线 — 「我的上传」的写路径（docs/LIBRARY_DESIGN.md §4）。

这条管线的形状是**契约**（§1-4），每一步都对应设计文档的一节：

1. 解码 —— UTF-8（含 BOM）→ UTF-16（**显式 BOM 门控**）→ GB18030 / Big5
   **评分择一**（可疑字符占比 >2% 判失败），全失败即单条失败；
2. 规范化 —— ``\\r\\n`` → ``\\n``、去首尾空白，字符数写 ``novel.text``；
3. 指纹 —— 规范化正文的 sha256 写 ``novel.source_key``（去重键）；
4. 去重 —— ``(source='local', source_key)`` 命中已有本地书 → 本条
   ``duplicate``，指出那本（§8-1：拒收 + 指路，不建重复行）；
5. 文件名解析 —— 标题 / 作者，``author_name`` **必须非 NULL**（解析不出写
   「未知作者」，§3.3：``sync_empty_name`` 扫 ``author_name IS NULL`` 再拿
   ``author_id`` 去 Pixiv 查作者名，非 NULL 即永不进该集合）；
6. 落库 —— ``id = -(10**9) + n``（n 越新越大，§3.1，取号走专用序列
   ``novel_local_id_seq``）、写 ``download/local/{安全标题}_{id}.txt``
   （原子写）、插行（``source='local'``、``has_epub=NO_IMAGES(3)``、
   ``create_time=now``、``like=view=0``、``author_id=series_id=NULL``）。

两条硬边界（§4）：

* **本地行永不进 ``_upsert_rows``**：本模块只做 INSERT，不 import 抓取路径的
  写仓库（``novels.repo.SQLAlchemyNovelWriteRepository`` / ``upsert_novels``；
  ``tests/architecture/test_library_isolation.py`` 用 AST 钉住）。
* **失败决不写 ``failed_novel``**：``failed_retry`` 会拿 ``failed_novel.novel_id``
  去 Pixiv 请求（``tasks/novels.py:366-390``），本地书进去就是必然失败的无意义
  请求。失败只逐条出现在响应体里（§4-7）。

幂等与并发：发号由专用序列 ``novel_local_id_seq`` 负责（并发安全、且**不回退**：
id 是用户可见的，复用会把旧引用指向另一本书）；写事务另外取一次
``pg_advisory_xact_lock`` —— 事务级咨询锁，提交/回滚即释放，职责是让
「指纹预检 + INSERT」串行（见 :func:`_lock_local_ids`），不是全局写锁
（postgres-migration 后 ``db_write()`` 已只是事务边界标记）。
"""

from __future__ import annotations

import codecs
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from copixiv.core.models import EpubStatus
from copixiv.core.services import safe_filename, sha256_text
from copixiv.db import models

# ---------------------------------------------------------------------------
# 常量（值域与 §2/§3 一致）
# ---------------------------------------------------------------------------

SOURCE_LOCAL = "local"

# 本地书 id 的专用序列（迁移 0005 创建：START -999999999 / INCREMENT 1）。
# ``nextval`` 并发安全且与事务无关 —— 回滚的尝试也消耗一个号，所以 id 永不
# 回退、永不复用（id 是用户可见的：DSL ``id:``、导出模板 ``{id}``）。
LOCAL_ID_SEQUENCE = "novel_local_id_seq"

# 本地书文件目录：download/local/（§3.2）。
LOCAL_SUBDIR = "local"

UNKNOWN_AUTHOR = "未知作者"
DECODE_ERROR = "无法识别编码（尝试 UTF-8 / GB18030 / Big5 / UTF-16）"
EMPTY_ERROR = "文件内容为空"

STATUS_CREATED = "created"
STATUS_DUPLICATE = "duplicate"
STATUS_FAILED = "failed"

# 咨询锁 key（事务级，hash 成 int4）。发号已经交给序列，这把锁现在的职责
# 只剩「指纹预检 + INSERT」的串行化 —— 见 :func:`_lock_local_ids`。
_ID_LOCK_KEY = "copixiv.library.local_novel_id"

# 解码顺序即契约 §4-1（Big5 与 GB18030 改为评分择一，见 decode_txt）。
# ``utf-8-sig`` = UTF-8(BOM)，无 BOM 时等同 ``utf-8``。
_UTF8 = "utf-8-sig"
# ``utf-16`` **必须显式 BOM 门控**：CPython 的 utf-16 解码器在缺少 BOM 时并不
# 报错，而是按本机字节序硬解（实测 `b"ab".decode("utf-16") == "扡"`，
# `b"\x80\x81"*8` → `"膀膀…"`）。不门控的话，任何偶数长度的二进制/Big5 字节流
# 都会在评分之前被静默吃成乱码 —— 正是本次要堵的坑。
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
# GB18030 与 Big5 的字节结构高度重叠，谁先谁后都会静默产乱码（Big5 字节被
# GB18030 解成 'и\ue019\ue1a8代刚' 一类），因此改为打分择一。
_SCORED_ENCODINGS = ("gb18030", "big5")

# 可疑字符占比上限：超过即判该候选不可信（乱码宁可报错，也不静默入库）。
MAX_SUSPICIOUS_RATIO = 0.02

# 可疑字符 = 私有区 Co / 未分配 Cn / 代理 Cs / 格式 Cf / 控制 Cc（\t \n \r 除外）
# + 替换符 U+FFFD（它的 Unicode 类别是 So，必须单独判）。
_SUSPICIOUS_CATEGORIES = frozenset({"Co", "Cn", "Cs", "Cf", "Cc"})
_BENIGN_CONTROL_CHARS = frozenset("\t\n\r")
_REPLACEMENT_CHAR = "\ufffd"


class TextDecodeError(ValueError):
    """所有候选编码都失败或不可信（单条 ``failed``，不写任何库表）。"""


# ---------------------------------------------------------------------------
# 1. 解码 / 2. 规范化 / 3. 指纹
# ---------------------------------------------------------------------------

def is_suspicious_char(char: str) -> bool:
    """该字符是否属于「解码错了」的强信号（乱码字形）。"""
    if char == _REPLACEMENT_CHAR:
        return True
    category = unicodedata.category(char)
    if category == "Cc":
        return char not in _BENIGN_CONTROL_CHARS
    return category in _SUSPICIOUS_CATEGORIES


def suspicious_ratio(text: str) -> float:
    """可疑字符占比（分母 = 正文总字符数，空串为 0.0）。"""
    if not text:
        return 0.0
    return sum(1 for char in text if is_suspicious_char(char)) / len(text)


def _scored_decode(data: bytes, encodings) -> tuple[str, float] | None:
    """在字节结构重叠的候选编码里挑「可疑字符最少」的那个。

    并列时取先者（``<`` 而非 ``<=``）—— 真 GBK/GB18030 文件在两个解码器下
    都可能零可疑字符（Big5 只是解成另一串「像中文的」错字），此时行为必须
    可预期地落在 ``gb18030``（列表第一个）。
    """
    best: tuple[str, float] | None = None
    for encoding in encodings:
        try:
            candidate = data.decode(encoding)
        except UnicodeError:
            continue
        ratio = suspicious_ratio(candidate)
        if best is None or ratio < best[1]:
            best = (candidate, ratio)
    return best


def decode_txt(data: bytes) -> str:
    """按契约 §4-1 解码；全部失败或不可信时抛 :class:`TextDecodeError`。

    1. ``utf-8-sig`` —— UTF-8（含 BOM）；
    2. ``utf-16`` —— **仅在带 BOM（FF FE / FE FF）时尝试**。CPython 的 utf-16
       解码器无 BOM 时按本机字节序硬解而不报错，所以「门控」必须自己写，
       否则偶数长度的二进制流会在这里被静默吃成乱码；
    3. ``gb18030`` / ``big5`` —— 评分择一：解出来的「可疑字符占比」更小者胜，
       并列取 ``gb18030``；最佳占比仍 > :data:`MAX_SUSPICIOUS_RATIO` 判失败。

    为什么第 3 步不能先到先得：GB18030 几乎不会抛错，排在 Big5 前面会把
    Big5 正文**静默**解成乱码（静默数据损坏比报错严重）。评分让两种编码各
    自证明自己，乱码一侧的私有区/未分配字符会把占比顶上去。

    已知边界（无新依赖前提下的固有缺口）：字节结构在 GB18030 下完全合法、
    解出来又「都是常用汉字」的异种编码（如部分 Shift-JIS 文本）仍会被当成
    GB18030 收下 —— 评分抓不住零可疑字符的错字，这需要真正的编码探测器。
    """
    try:
        return data.decode(_UTF8)
    except UnicodeError:
        pass

    if data.startswith(_UTF16_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass

    best = _scored_decode(data, _SCORED_ENCODINGS)
    if best is None or best[1] > MAX_SUSPICIOUS_RATIO:
        raise TextDecodeError(DECODE_ERROR)
    return best[0]


def normalize_text(text: str) -> str:
    """规范化：``\\r\\n`` → ``\\n``，去首尾空白（§4-2）。"""
    return text.replace("\r\n", "\n").strip()


def body_fingerprint(normalized: str) -> str:
    """规范化正文的 sha256（§4-3）—— 去重键，也是将来的合并锚。

    与抓取路径的变更检测（``features/novels/download_novel.py``）**输入不同**：
    这里算的是 ``normalize_text`` 之后的正文（``\\r\\n`` → ``\\n``、去首尾空白），
    因为去重键必须让同一份内容的两种换行写法得到同一个值；那条路径算的是
    **写盘原文**，因为它比的是磁盘上那份文件。两者共用
    :func:`copixiv.core.services.sha256_text` 同一个哈希原语，归一化各自负责。
    """
    return sha256_text(normalized)


# ---------------------------------------------------------------------------
# 5. 文件名解析：标题 / 作者
# ---------------------------------------------------------------------------

# ``【作者】标题`` / ``[作者] 标题`` —— 方括号（中英文）打头即作者。
_BRACKET_RE = re.compile(
    r"^\s*[【\[]\s*(?P<author>[^】\]]+?)\s*[】\]]\s*(?P<title>.+?)\s*$"
)

# ``作者 - 标题`` 的分隔符：半角/全角连字符**两侧要有空白**，破折号（– —）
# 可无空白。半角 ``-`` 不带空白时不当分隔符 —— 否则
# ``魔法少女-小圆.txt`` 会被误拆成作者「魔法少女」。
_DASH_SEPARATORS = (" - ", " － ", " – ", " — ", " –", "—", "–")

# 「(完结)」这类尾部状态标记：只有括号内容命中已知标记才剥掉，避免误删标题。
_STATUS_MARKERS = frozenset({
    "完结", "已完结", "完结文", "全本", "全集", "完", "连载", "未完结",
    "txt", "精校", "校对",
})
_TRAILING_GROUP_RE = re.compile(r"[（(\[【]\s*([^）)\]】]{0,16}?)\s*[）)\]】]\s*$")


def _strip_status_markers(title: str) -> str:
    """剥掉尾部的 ``(完结)`` / ``（全本）`` / ``[txt]`` 一类状态标记。"""
    current = title.strip()
    while True:
        match = _TRAILING_GROUP_RE.search(current)
        if not match:
            return current
        if match.group(1).strip().lower() not in _STATUS_MARKERS:
            return current
        stripped = current[: match.start()].strip()
        if not stripped:
            return current
        current = stripped


def _split_dash(stem: str) -> tuple[str, str] | None:
    """``作者 - 标题`` → ``(author, title)``；没有分隔符返回 None。"""
    for separator in _DASH_SEPARATORS:
        index = stem.find(separator)
        if index <= 0:
            continue
        author = stem[:index].strip()
        title = stem[index + len(separator):].strip()
        if author and title:
            return author, title
    return None


def clean_filename(filename: str) -> str:
    """取出文件名本体（去掉上传客户端可能带上的目录部分）。"""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "未命名.txt"


def parse_filename(filename: str) -> tuple[str, str]:
    """从文件名解析 ``(title, author_name)``。

    支持 ``【作者】标题(完结).txt`` / ``[作者] 标题.txt`` / ``作者 - 标题.txt``
    / 纯 ``标题.txt``（§4-5）。**``author_name`` 永不为空**：解析不出即
    「未知作者」——这是隔离 ``sync_empty_name`` 任务的关键（§3.3）。
    """
    stem = clean_filename(filename)
    if stem.lower().endswith(".txt"):
        stem = stem[:-4]
    stem = stem.strip()

    author = ""
    title = stem
    match = _BRACKET_RE.match(stem)
    if match:
        author, title = match.group("author"), match.group("title")
    else:
        split = _split_dash(stem)
        if split:
            author, title = split

    title = _strip_status_markers(title).strip() or title.strip() or stem
    return title, (author.strip() or UNKNOWN_AUTHOR)


# ---------------------------------------------------------------------------
# 6. id / 路径 / 落库
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestResult:
    """逐条上传结果（响应契约见 §4 / ``features/library/schemas.py``）。"""

    filename: str
    status: str
    novel_id: int | None = None
    title: str | None = None
    author_name: str | None = None
    text: int | None = None
    error: str | None = None


def local_novel_path(download_dir: str, title: str, novel_id: int) -> Path:
    """``{download_dir}/local/{安全标题}_{id}.txt``（§3.2）。

    标题按 ``NAME_MAX`` 截断，连 ``.tmp`` 临时后缀一起留出余量 —— 与
    ``core/services.py::build_path`` 同一套算式（本地书自己拼路径，不走
    ``build_path``：它按 id 分片，负数会被塞进 ``0000/``，而 §3.2 要求的
    是 ``local/``）。
    """
    reserved = len(f"_{novel_id}.epub.tmp") + 1
    filename = f"{safe_filename(title, max_length=254 - reserved)}_{novel_id}.txt"
    return Path(download_dir) / LOCAL_SUBDIR / filename


def _write_text_atomic(root: Path, path: Path, content: str) -> None:
    """临时文件 + ``os.replace`` 原子写；目录必须落在 download 根内（§3.2）。"""
    resolved_root = root.resolve()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.resolve().is_relative_to(resolved_root):
        raise OSError(f"路径落在 download 根之外：{path}")

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _lock_local_ids(session) -> None:
    """取事务级咨询锁 —— 现在只负责让「指纹预检 + INSERT」在并发下串行。

    发号已经交给专用序列（``nextval`` 并发安全且不回退），所以这把锁**不再**
    是 id 分配的一部分；它挡住的是去重竞态：两个并发上传同一份正文时，若都
    通过了预检，后者会在 ``ux_novel_source_key`` 上撞车。PG 里一条语句报错会
    把**整个事务**置为 aborted，而整批上传共用一个事务
    （``Depends(get_write_uow)``），后面本来合法的文件会被一起拖死。

    没有改成「每个文件一个 savepoint + 靠唯一索引兜底」：那要每次上传都多开
    savepoint，换来的只是更少的锁 —— 上传是低频人工操作，不划算。插入处的
    ``begin_nested()`` 因此只当**兜底**（单条撞车不毒化整批），去重的正确性
    由预检 + 这把锁保证。
    """
    session.execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": _ID_LOCK_KEY},
    )


def _next_local_id(session) -> int:
    """从专用序列取下一个本地书 id（§3.1；序列由迁移 0005 创建）。

    ``nextval`` 与事务无关：回滚的尝试也消耗一个号 —— 这正是「id 永不复用」
    要的语义（用户可见的 id 复用会把旧引用指向另一本书）。
    """
    return int(session.execute(
        select(func.nextval(LOCAL_ID_SEQUENCE))
    ).scalar_one())


def _find_local_duplicate(session, source_key: str):
    """``(source='local', source_key)`` 命中的已有本地书 → ``(id, title)``。"""
    return session.execute(
        select(models.Novel.id, models.Novel.title).where(
            models.Novel.source == SOURCE_LOCAL,
            models.Novel.source_key == source_key,
        )
    ).first()


def ingest_uploads(
    session,
    download_dir: str,
    files: list[tuple[str, bytes]],
) -> list[IngestResult]:
    """把一批 ``(filename, raw_bytes)`` 收进库，逐条返回结果。

    调用方负责事务边界（API 侧是 ``Depends(get_write_uow)``）：本函数只
    INSERT，不 commit / rollback。**部分成功**：一条失败不影响其余条，
    失败条目不写任何库表（尤其不写 ``failed_novel``）。
    """
    root = Path(download_dir)
    results: list[IngestResult] = []
    # 锁懒初始化：整批全是解码失败时不碰数据库。
    locked = False

    for filename, data in files:
        name = clean_filename(filename)

        try:
            raw = decode_txt(data)
        except TextDecodeError:
            results.append(IngestResult(name, STATUS_FAILED, error=DECODE_ERROR))
            continue

        body = normalize_text(raw)
        if not body:
            results.append(IngestResult(name, STATUS_FAILED, error=EMPTY_ERROR))
            continue

        source_key = body_fingerprint(body)

        if not locked:
            _lock_local_ids(session)
            locked = True

        existing = _find_local_duplicate(session, source_key)
        if existing is not None:
            results.append(IngestResult(
                name, STATUS_DUPLICATE,
                novel_id=int(existing[0]), title=existing[1],
            ))
            continue

        title, author_name = parse_filename(name)

        # 发号在去重预检之后：重复件不消耗号（真撞车时号会浪费一个，这是
        # 「永不复用」的必然代价）。
        novel_id = _next_local_id(session)

        path = local_novel_path(download_dir, title, novel_id)
        try:
            _write_text_atomic(root, path, body)
        except OSError as exc:
            results.append(IngestResult(
                name, STATUS_FAILED, error=f"写入文件失败：{exc}",
            ))
            continue

        novel = models.Novel(
            id=novel_id,
            title=title,
            author_id=None,
            author_name=author_name,
            path=str(path),
            like=0,
            view=0,
            text=len(body),
            caption=None,
            series_id=None,
            series_name=None,
            series_index=None,
            create_time=datetime.now(timezone.utc),
            # 终态：本地书永远不会有抓取路径去构建 EPUB（§3.3）。
            has_epub=EpubStatus.NO_IMAGES,
            shuffle=_random_shuffle(),
            tags=[],
            is_favourite=False,
            source=SOURCE_LOCAL,
            source_key=source_key,
        )
        try:
            # 兜底 savepoint：单条撞车也只回滚这条，本批其余条不受影响
            # （PG 里一条语句报错会让整个事务进入 aborted 状态）。去重的
            # 正确性由 _lock_local_ids 的预检保证，这里只是不毒化整批。
            with session.begin_nested():
                session.add(novel)
                session.flush()
        except IntegrityError:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            results.append(IngestResult(
                name, STATUS_FAILED,
                error="入库冲突：id 或正文指纹与已有记录撞车",
            ))
            continue

        results.append(IngestResult(
            name, STATUS_CREATED,
            novel_id=novel_id, title=title, author_name=author_name,
            text=len(body),
        ))

    return results


def _random_shuffle() -> int:
    """随机浏览键 —— 与 ``_upsert_rows`` 新建行时的约定一致（列默认 0）。"""
    import random

    return random.randint(0, 2**31 - 1)
