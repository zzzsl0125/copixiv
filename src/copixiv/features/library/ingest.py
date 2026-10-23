"""本地书籍入库管线 — 「我的上传」的写路径（docs/LIBRARY_DESIGN.md §4）。

这条管线的形状是**契约**（§1-4），每一步都对应设计文档的一节：

1. 解码 / 读取 —— **TXT**：UTF-8（含 BOM）→ UTF-16（**显式 BOM 门控**）→
   GB18030 / Big5 **评分择一**（可疑字符占比 >2% 判失败），全失败即单条失败；
   **EPUB**（§4.1）：按内容/后缀分派给 :mod:`copixiv.storage.epub.reader`，抽出
   正文与 ``dc:title`` / ``dc:creator``，读不出来（坏容器 / 无正文）同样单条
   失败。两条路在 :func:`parse_upload` 里汇成一个 :class:`ParsedUpload`，
   之后整条管线**对格式无感知**；
2. 规范化 —— ``\\r\\n`` → ``\\n``、去首尾空白，字符数写 ``novel.text``；
3. 指纹 —— 规范化正文的 sha256 写 ``novel.source_key``（去重键）—— **两种格式
   共用同一套指纹**，所以"同一份正文的 TXT 与 EPUB"仍然互相命中（§4.1）；
4. 去重 —— ``(source='local', source_key)`` 命中已有本地书 → 本条
   ``duplicate``，指出那本（§8-1：**不建重复行**、指路已有那本；但"整批预设
   作者"与"EPUB 补挂"仍会写库，各自回显一个可选字段）；
   **EPUB 够格就补挂**（§4.1）：命中行还没有 EPUB 原件时，把这次的原件写到
   它的 ``.epub`` 兄弟路径并置 ``has_epub=DONE``，响应多一个
   ``epub_attached`` 标记；
5. 文件名解析 —— 标题 / 作者，``author_name`` **必须非 NULL**（解析不出写
   「未知作者」）。这是 §3.3 产品承诺「每本书都有作者名可显示」在入库侧的落点，
   **不外包给任务**：``sync_empty_name`` 扫的确实是 ``author_name IS NULL`` 的
   行，但它默认禁用（``scheduled_task.is_enabled`` 列默认 false，只有手动启用
   才跑）——预设作者是唯一会**覆盖**这一步的输入（见下一节）。EPUB 元数据优先
   于文件名解析（``dc:title`` / ``dc:creator``），缺失才回落（
   :func:`resolve_metadata`）；
6. 落库 —— ``id = -(坐标)``（§3.1：坐标 = 上传时刻在 pixiv 作品号轴上的位置，
   见 :mod:`copixiv.features.library.local_axis`）、写
   ``download/local/{安全标题}_{id}.txt``（原子写）；EPUB 再把**原件**写到同级
   的 ``{…}_{id}.epub``（读路径拿 ``path`` 换后缀就能取到，下游零改动）。
   插行（``source='local'``、``has_epub=NO_IMAGES(3)``（TXT）/ ``DONE(2)``
   （EPUB，原件已在盘上）、``create_time=now``、``like=view=0``、
   ``series_id=NULL``；``author_id`` 没给预设时是 NULL）。

预设作者（``preset_author_id`` / ``preset_author_name``，2026-09 加）：一次上传
的**整批书**可以挂同一位作者。三件事是刻意的，各有理由：

* **预设优先于文件名解析**。文件名里的作者是用户**随手起的**（甚至只是下载站
  的命名习惯），预设 id 是用户在界面上**明确指定**的身份；两者冲突时以明确
  指定的为准——id 是身份、名字是它的缓存，与单本 ``PATCH``
  （``novels/update_metadata.py::_author_columns``）和批量编辑
  （``novels/batch_edit_metadata.py``）逐字同一套口径。所以给了 ``author_id``
  时，这一批的 ``author_name`` **不再取** ``parse_filename`` 的结果。
* **解析不出名字就写 ``NULL``（显式写，覆盖文件名解析值）**，既不退回文件名里
  那个名字、也不写「未知作者」。``author_id`` 已经落库，名字只是它的缓存：退回
  文件名里的名字会让这一行显示成"id 指向 A、名字写着 B"，是一张自相矛盾的身份
  映射；写「未知作者」则等于用一个看起来确定的假名盖住"待解析"。``NULL`` 是
  仓库**认可的中间态**——**别把它说成"有兜底任务"**：``sync_empty_name`` 默认
  禁用（``scheduled_task.is_enabled`` 列默认 false，只有手动启用才跑），会把
  名字补回来的是**再抓一次该作者 / 作品**或用户重新「同步」。
* **重复件也应用**（用户 2026-09 拍板）。用户的动作是"把这批书挂到这位作者
  名下"，重复件已经在库并不改变这个意图；不应用就是"用户以为挂上了、其实没
  挂"——静默不做比报错更糟。同时保留"重复件尽量不动库"的既有精神：``author_id``
  与解析出的名字**本来就一模一样**时一个字节都不写（值级断言据此成立）。

两条硬边界（§4）：

* **本地行永不进 ``_upsert_rows``**：本模块只写 ``novel``（新书 INSERT；重复件
  只改 ``author_id`` / ``author_name`` 两列）与 ``author`` 行的名字
  （``update_author_name_sync``，与单本 / 批量编辑同一个收尾），不 import 抓取
  路径的写仓库（``novels.repo.SQLAlchemyNovelWriteRepository`` /
  ``upsert_novels``；``tests/architecture/test_library_isolation.py`` 用 AST
  钉住）。
* **失败决不写 ``failed_novel``**：``failed_retry`` 会拿 ``failed_novel.novel_id``
  去 Pixiv 请求（``tasks/novels.py:366-390``），本地书进去就是必然失败的无意义
  请求。失败只逐条出现在响应体里（§4-7）。

幂等与并发：发号由 :func:`local_axis.next_local_id` 负责 —— 坐标游标放在专用
序列 ``novel_local_coord_seq`` 上，``nextval`` 并发安全且**不回退**（id 是用户
可见的，复用会把旧引用指向另一本书）；写事务另外取一次
``pg_advisory_xact_lock`` —— 事务级咨询锁，提交/回滚即释放，职责是让
「指纹预检 + INSERT」串行（见 :func:`_lock_local_ids`），不是全局写锁
（postgres-migration 后 ``db_write()`` 已只是事务边界标记）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from copixiv.core import formats
from copixiv.core.models import EpubStatus, new_shuffle_key
from copixiv.core.services import sha256_text
from copixiv.db import models
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.library import local_axis
from copixiv.features.library.decode import (
    DECODE_ERROR,
    MAX_SUSPICIOUS_RATIO,
    TextDecodeError,
    decode_txt,
    is_suspicious_char,
    suspicious_ratio,
)
from copixiv.features.library.filenames import (
    UNKNOWN_AUTHOR,
    clean_filename,
    parse_filename,
)
from copixiv.log import logger
from copixiv.storage import atomic
from copixiv.storage.epub import reader
from copixiv.storage.file_storage import FileStorage

# 解码 / 文件名解析的公开名字在 ``decode.py`` / ``filenames.py``（各自是唯一
# 所有者）；这里 re-export 只是为了让既有调用点与测试的 import 路径不变。
__all__ = [
    "DECODE_ERROR",
    "EMPTY_ERROR",
    "IngestResult",
    "MAX_SUSPICIOUS_RATIO",
    "ParsedUpload",
    "STATUS_CREATED",
    "STATUS_DUPLICATE",
    "STATUS_FAILED",
    "TextDecodeError",
    "UNKNOWN_AUTHOR",
    "UploadStatus",
    "UploadUnreadable",
    "body_fingerprint",
    "clean_filename",
    "decode_txt",
    "ingest_uploads",
    "is_suspicious_char",
    "normalize_text",
    "parse_filename",
    "parse_upload",
    "resolve_metadata",
    "suspicious_ratio",
]

# ---------------------------------------------------------------------------
# 常量（值域与 §2/§3 一致）
# ---------------------------------------------------------------------------

SOURCE_LOCAL = local_axis.SOURCE_LOCAL

EMPTY_ERROR = "文件内容为空"

UploadStatus = Literal["created", "duplicate", "failed"]
STATUS_CREATED: UploadStatus = "created"
STATUS_DUPLICATE: UploadStatus = "duplicate"
STATUS_FAILED: UploadStatus = "failed"

# 咨询锁 key（事务级，hash 成 int4）。发号已经交给序列，这把锁现在的职责
# 只剩「指纹预检 + INSERT」的串行化 —— 见 :func:`_lock_local_ids`。
_ID_LOCK_KEY = "copixiv.library.local_novel_id"


# ---------------------------------------------------------------------------
# 1b. 分派与统一解析 —— TXT / EPUB 在这里汇成一个形状，之后的管线格式无关
# ---------------------------------------------------------------------------

class UploadUnreadable(ValueError):
    """本文件读不出可用正文（TXT 解码失败 / EPUB 读失败）—— 单条 ``failed``。

    存在的意义是让循环只写一个 ``except``：两条路的失败原因都已经是一句
    给用户看的中文，循环不需要（也不该）知道它是编码问题还是容器问题。
    """


@dataclass(frozen=True)
class ParsedUpload:
    """一个上传文件解析后的形状 —— 管线其余部分只认它。

    ``body`` **未规范化**（调用方走 :func:`normalize_text`，与指纹口径一致）；
    ``original`` 是 EPUB 原件字节（TXT 为 ``None``），"是不是 EPUB"从此只有
    这一个判据，不必再回看文件名。
    """

    body: str
    title: str | None = None
    author_name: str | None = None
    original: bytes | None = None


def parse_upload(name: str, data: bytes) -> ParsedUpload:
    """把 ``(文件名, 字节)`` 读成 :class:`ParsedUpload`；失败抛
    :class:`UploadUnreadable`。

    分派判据是**内容 + 后缀**（:func:`copixiv.storage.epub.reader.looks_like_epub`）：
    改名的 EPUB 也能读，``.txt`` 不可能同时是那种 zip，所以不误伤。
    """
    if reader.looks_like_epub(name, data):
        try:
            content = reader.read_epub(data)
        except reader.EpubReadError as exc:
            raise UploadUnreadable(str(exc)) from exc
        return ParsedUpload(
            body=content.body,
            title=content.title,
            author_name=content.author_name,
            original=data,
        )

    try:
        return ParsedUpload(body=decode_txt(data))
    except TextDecodeError as exc:
        raise UploadUnreadable(DECODE_ERROR) from exc


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


def resolve_metadata(filename: str, parsed: ParsedUpload) -> tuple[str, str]:
    """``(title, author_name)`` —— EPUB 元数据优先，文件名解析兜底（§4.1）。

    * 标题：``dc:title`` > 文件名解析 > 文件名本体；
    * 作者：``dc:creator`` > 文件名解析 > 「未知作者」。

    TXT 的 :class:`ParsedUpload` 两个元数据字段是 ``None``，于是自然走文件名
    解析——两种格式共用这一个函数，不再需要"epub_meta 是不是 None"的分支。

    为什么不在这里处理预设作者：预设是**整批**的显式输入、且要覆盖这里解出来
    的作者名，属于调用方（``ingest_uploads``）的循环里那一步，不是"来源优先级"
    的一档。名字为空的 ``dc:creator``（或压根没有）按"没给"处理，落到文件名。
    """
    title, author_name = parse_filename(filename)
    return (
        (parsed.title or "").strip() or title,
        (parsed.author_name or "").strip() or author_name,
    )


# ---------------------------------------------------------------------------
# 6. id / 路径 / 落库
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestResult:
    """逐条上传结果（响应契约见 §4 / ``features/library/schemas.py``）。

    ``epub_attached`` 只在**重复件补挂 EPUB** 时置 ``True``（§4.1）：正文指纹
    命中已有本地书、而来的是 EPUB 且那一行还没有 EPUB 时，把原件挂上去并置
    ``has_epub=DONE``。其余情形保持 ``None``（响应里不下发）。
    """

    filename: str
    status: UploadStatus
    novel_id: int | None = None
    title: str | None = None
    author_name: str | None = None
    text: int | None = None
    error: str | None = None
    epub_attached: bool | None = None


def _lock_local_ids(session) -> None:
    """取事务级咨询锁 —— 现在只负责让「指纹预检 + INSERT」在并发下串行。

    发号不靠这把锁（``nextval`` 本身原子且不回退，见
    :func:`local_axis.next_local_id`）；它挡住的是去重竞态：两个并发上传同一份
    正文时，若都通过了预检，后者会在 ``ux_novel_source_key`` 上撞车。PG 里一条
    语句报错会把**整个事务**置为 aborted，而整批上传共用一个事务
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


def _find_local_duplicate(session, source_key: str):
    """``(source='local', source_key)`` 命中的已有本地书 → ``(id, title)``。"""
    return session.execute(
        select(models.Novel.id, models.Novel.title).where(
            models.Novel.source == SOURCE_LOCAL,
            models.Novel.source_key == source_key,
        )
    ).first()


def _apply_preset_to_duplicate(
    session,
    novel: models.Novel | None,
    preset_author_id: int,
    preset_author_name: str | None,
) -> bool:
    """把预设作者挂到**已在库**的那一行上；返回"这次真的写了吗"。

    为什么这里允许逐条改字段（本仓「批量必须 set-based」那条纪律的例外，取舍
    写清楚）：那条纪律针对的是**几百上千本**的批量编辑
    （``novels/batch_edit_metadata.py`` 的 ④：逐行 ``get_by_id`` + ``setattr``
    正是它要避免的形态），而这里的行集上限是"用户这次拖进上传框的文件数"，而且
    只有其中的**重复件**才走这条分支——去重预检本来就已经按文件逐条 SELECT，
    这一条只是给命中的那一行改两列。名字的**跨行规范化**仍然走
    ``update_author_name`` 那种 set-based 写法（见函数末尾），不在循环里干。

    ``novel`` 为 ``None``（预检看到、加载前被并发删掉）时返回 ``False``——
    这一条仍按 ``duplicate`` 上报（预检的事实成立），只是没有行可挂。

    不写的情形只有一个：``author_id`` 与 ``author_name`` **本来就和要写的一模
    一样**——「重复件尽量不动库」的既有精神（也让"已挂同一作者时不写"可断言）。
    """
    if novel is None:
        return False
    if (
        novel.author_id == preset_author_id
        and novel.author_name == preset_author_name
    ):
        return False

    novel.author_id = preset_author_id
    novel.author_name = preset_author_name
    # 立刻 flush：一是让紧随其后的 ``update_author_name`` 的 ``IS DISTINCT
    # FROM`` 在同一事务里看得见这一行（与本批新书在 ``begin_nested`` 里 flush
    # 同一形态）；二是外键违规（``preset_author_id`` 没有 author 行）在这里就
    # 暴露，而不是拖到提交时把报错点挪到别的地方。
    session.flush()
    return True


def _attach_epub_to_existing(
    session, novel: models.Novel | None, root: Path, epub_bytes: bytes,
) -> bool:
    """把上传的 EPUB 原件补挂到**已在库**的本地书上；返回"这次真的挂了吗"。

    §4.1 的"够格就补挂"：正文指纹命中意味着这本书已经在库里（多半是以前传的
    TXT）；如果它还没有 EPUB，就把这次的原件写到它的 ``.epub`` 兄弟路径并置
    ``has_epub=DONE``——用户因此能把更完整的原件补进已有条目，而不是被
    ``duplicate`` 挡在门外。

    不挂的三种情形，逐条都有理由：

    * 那一行已经不在了（预检看到后被并发删掉）——没有行可挂；
    * 已经是 ``DONE`` **且文件还在**——一个字节都不写（"重复件尽量不动库"的
      既有精神，也让"已挂同一份原件时不写"可断言）；
    * 写盘失败——只记 warning、返回 ``False``。**绝不**把这条降级成
      ``failed``：书确实已经在库里，把"补挂附件失败"说成"这本没进去"是更糟的
      谎；响应仍是 ``duplicate``，只是不带补挂标记。
    """
    if novel is None or not novel.path:
        return False

    path = formats.sibling_path(Path(novel.path), formats.EPUB)
    if novel.has_epub == EpubStatus.DONE and path.is_file():
        return False

    try:
        atomic.write_atomic(path, epub_bytes, root=root)
    except OSError as exc:
        logger.warning(
            "EPUB 补挂失败（书已在库，按 duplicate 上报）: #{} {}: {}",
            novel.id, path, exc,
        )
        return False

    # has_epub 是派生状态（docs/epub-status-fix-contract.md）：**产出该 EPUB 的
    # 路径**写出成品后自己置位。这里的"产出方"就是这次上传——文件确实落盘了，
    # 置 DONE 是如实记录；负 id 不在 check_epub 的巡检区间（cursor 从 0 起），
    # 不会被改回。
    novel.has_epub = EpubStatus.DONE
    session.flush()
    return True


def _read_upload(name: str, data: bytes) -> tuple[str, ParsedUpload]:
    """读一份上传 → ``(规范化正文, 解析结果)``；读不出就抛
    :class:`UploadUnreadable`（消息已是给用户看的一句中文）。
    
    把「解码 / 抽取」「规范化」「空正文」三步收成一处，于是循环只需要一个
    ``except``：TXT 的编码失败、EPUB 的容器失败、空正文对调用方是同一类事实
    —— 这条没进库。
    """
    parsed = parse_upload(name, data)
    body = normalize_text(parsed.body)
    if not body:
        raise UploadUnreadable(EMPTY_ERROR)
    return body, parsed


def _discard(*paths: Path | None) -> None:
    """删掉这次写了一半的文件（EPUB 写失败 / 撞车时）。

    ``missing_ok`` + 吞 ``OSError``：清理是尽力而为的收尾，它自己失败不该把
    一条本来就有结论的结果变成异常。
    """
    for written in filter(None, paths):
        try:
            written.unlink(missing_ok=True)
        except OSError:
            pass


def _write_files(
    storage: FileStorage,
    novel_id: int,
    title: str,
    body: str,
    original: bytes | None,
    root: Path,
) -> tuple[Path, Path | None]:
    """把正文（与 EPUB 原件）落盘 → ``(正文路径, 原件路径或 None)``。

    失败时先把**已经写成的那些**删掉再抛出：正文落盘、原件失败会留下一本
    "没有原件却像是成功了"的孤儿 txt。两件事合在一处，是因为"写了哪份"
    只有这里知道；调用方只需要决定这一条算不算失败。
    """
    path = storage.local_novel_path(novel_id, title)
    # 原件存成同级兄弟文件（``{…}_{id}.epub``）——读路径
    # （GetNovelFileUseCase / 批量导出）就是拿 ``path`` 换后缀找它，下游零改动。
    epub_path = (
        storage.local_novel_path(novel_id, title, formats.EPUB)
        if original is not None
        else None
    )
    try:
        atomic.write_text_atomic(path, body, root=root)
        if epub_path is not None and original is not None:
            atomic.write_atomic(epub_path, original, root=root)
    except OSError:
        _discard(path, epub_path)
        raise
    return path, epub_path


def _handle_duplicate(
    session,
    existing_id: int,
    existing_title: str | None,
    parsed: ParsedUpload,
    name: str,
    *,
    preset_author_id: int | None,
    preset_author_name: str | None,
    root: Path,
) -> tuple[IngestResult, bool]:
    """已在库的那一条 → ``(结果, 这次是否真的写了预设作者)``。"""
    # 只有真的要动那一行时才加载它（纯重复件仍是一次 SELECT 了事）——
    # 两个 helper 收同一个 ORM 对象，不再各自 ``session.get`` 一次。
    needs_row = preset_author_id is not None or parsed.original is not None
    novel = session.get(models.Novel, existing_id) if needs_row else None

    wrote_preset = False
    duplicate_author_name: str | None = None
    if preset_author_id is not None:
        # 应用预设（重复件也应用）。没给预设时整段不进 —— 那就是既有形状：
        # 一个字节都不改、也不回显作者名。
        wrote_preset = _apply_preset_to_duplicate(
            session, novel, preset_author_id, preset_author_name,
        )
        # 回显这次写进去的名字（解析不出就是 None，响应里该字段不下发）——
        # 前端据此显示「已在库中《…》· 已挂作者：X」。
        duplicate_author_name = preset_author_name

    # 够格就补挂（§4.1）：来的是 EPUB 且那一行还没有 EPUB 时，把原件挂上去。
    # TXT 不参与（没有原件可挂）。
    epub_attached = (
        _attach_epub_to_existing(session, novel, root, parsed.original)
        if parsed.original is not None
        else False
    )
    return IngestResult(
        name, STATUS_DUPLICATE,
        novel_id=existing_id, title=existing_title,
        author_name=duplicate_author_name,
        epub_attached=epub_attached or None,
    ), wrote_preset


def _create_row(
    session,
    storage: FileStorage,
    name: str,
    body: str,
    parsed: ParsedUpload,
    source_key: str,
    *,
    preset_author_id: int | None,
    preset_author_name: str | None,
    root: Path,
) -> tuple[IngestResult, bool]:
    """新书：取号 → 落盘 → 插行 → ``(结果, 这次是否真的写了预设作者)``。"""
    title, parsed_author_name = resolve_metadata(name, parsed)
    if preset_author_id is None:
        author_id, author_name = None, parsed_author_name
    else:
        # 预设作者：连名字一起显式写（``None`` 也写 —— 那是"待解析"，
        # 见模块 docstring 的「预设作者」一节，不退回文件名解析值）。
        author_id, author_name = preset_author_id, preset_author_name

    # 发号在去重预检之后：重复件不消耗号（真撞车时号会浪费一个，
    # 这是「永不复用」的必然代价）。实现与契约见 local_axis。
    novel_id = local_axis.next_local_id(session)

    try:
        path, epub_path = _write_files(
            storage, novel_id, title, body, parsed.original, root,
        )
    except OSError as exc:
        return IngestResult(
            name, STATUS_FAILED, error=f"写入文件失败：{exc}",
        ), False

    novel = models.Novel(
        id=novel_id,
        title=title,
        author_id=author_id,
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
        # EPUB 上传：原件已在上面落盘，``DONE`` 是如实记录；TXT 上传仍是
        # 无图终态（本地行不会被抓取路径构建 EPUB）。两种都不在 check_epub
        # 的巡检区间（负 id，cursor 从 0 起）。
        has_epub=(
            EpubStatus.DONE if epub_path is not None else EpubStatus.NO_IMAGES
        ),
        shuffle=new_shuffle_key(),
        tags=[],
        is_favourite=False,
        source=SOURCE_LOCAL,
        source_key=source_key,
    )
    try:
        # 兜底 savepoint：单条撞车也只回滚这条，本批其余条不受影响（PG 里一条
        # 语句报错会让整个事务进入 aborted 状态）。去重的正确性由
        # ``_lock_local_ids`` 的预检保证，这里只是不毒化整批。
        with session.begin_nested():
            session.add(novel)
            session.flush()
    except IntegrityError:
        # 撞车的那条也把两份盘上文件清干净（正文 + 可能已写的 EPUB）。
        _discard(path, epub_path)
        return IngestResult(
            name, STATUS_FAILED,
            error="入库冲突：id 或正文指纹与已有记录撞车",
        ), False

    return IngestResult(
        name, STATUS_CREATED,
        novel_id=novel_id, title=title, author_name=author_name,
        text=len(body),
    ), preset_author_id is not None


def ingest_uploads(
    session,
    storage: FileStorage,
    files: list[tuple[str, bytes]],
    *,
    preset_author_id: int | None = None,
    preset_author_name: str | None = None,
) -> list[IngestResult]:
    """把一批 ``(filename, raw_bytes)`` 收进库，逐条返回结果。

    调用方负责事务边界（API 侧是 ``write_uow.begin()``）：本函数只写库，
    不 commit / rollback。**部分成功**：一条失败不影响其余条，失败条目不写任何
    库表（尤其不写 ``failed_novel``，失败时也不会去动 ``author`` 行）。

    ``storage`` 是路径与落盘的**唯一所有者**（``FileStorage``）：本函数不再自己
    拼 ``download/local/...``，也不再自带原子写——那两件事各只有一处实现。

    ``preset_author_id`` / ``preset_author_name`` 可选、**整批共用**：给了 id 就
    把这一批全部挂到它名下（覆盖文件名解析出的作者名；名字可以是 ``None``，
    含义见模块 docstring 的「预设作者」一节）。两条前置约定：

    * 名字解析**不在这里做**（可能在写事务外问一次 pixiv，见
      ``features/library/api.py``）——本函数只消费调用方解析好的结果，所以
      "解析只做一次"是调用方的形状，不是这里的循环；
    * ``preset_author_id`` 的 ``author`` 占位行由调用方保证存在
      （``author_id`` 有外键；API 侧在读事务里建、写事务里再兜一次）。
    """
    if preset_author_id is None:
        # 名字是 id 的派生值：没有 id 就没有"预设作者"这回事。调用方误传一个
        # 孤零零的名字时按"没给预设"处理，免得出现"有名字没 id"的行。
        preset_author_name = None

    root = Path(storage.download_dir)
    results: list[IngestResult] = []
    # 锁与预设作者的占位行都**懒初始化**：整批全是解码失败时不碰数据库。
    prepared = False
    # 这批是否真的把预设作者写进了某些行 —— 决定收尾要不要回写 author 行。
    preset_written = False

    for filename, data in files:
        name = clean_filename(filename)
        try:
            body, parsed = _read_upload(name, data)
        except UploadUnreadable as exc:
            results.append(IngestResult(name, STATUS_FAILED, error=str(exc)))
            continue

        # 拿到第一条能读的文件才碰数据库：锁挡的是「指纹预检 + INSERT」的
        # 并发竞态；整批都读不出来时这两步都没必要。
        if not prepared:
            _lock_local_ids(session)
            if preset_author_id is not None:
                # 外键兜底。API 侧已经建过占位行，但那是一条**不可执行**的
                # 约定（本函数是公开的，直接调用时没有那一步），而违约的后果
                # 不是"这一条失败"而是"整批都报入库冲突"——``session.flush()``
                # 里的 FK 违例被下面的 ``IntegrityError`` 分支接住，消息却指向
                # 一个与事实无关的原因。``ON CONFLICT DO NOTHING`` 补一次，
                # 与 ``persist_novels`` 的 ``ensure_exists`` 同一姿势。
                SQLAlchemyAuthorRepository(session).ensure_exists(
                    {preset_author_id}
                )
            prepared = True

        source_key = body_fingerprint(body)
        existing = _find_local_duplicate(session, source_key)
        if existing is not None:
            result, wrote_preset = _handle_duplicate(
                session, int(existing[0]), existing[1], parsed, name,
                preset_author_id=preset_author_id,
                preset_author_name=preset_author_name,
                root=root,
            )
        else:
            result, wrote_preset = _create_row(
                session, storage, name, body, parsed, source_key,
                preset_author_id=preset_author_id,
                preset_author_name=preset_author_name,
                root=root,
            )
        # 失败的那条不写表，也就不该触发作者行收尾。
        preset_written = preset_written or wrote_preset
        results.append(result)

    # 作者行收尾（最后做一次，不是每条做一次）：``update_author_name`` 写
    # ``author`` 行**并**用 ``IS DISTINCT FROM`` 规范化同作者的全部 novel 行
    # （刚写的这批会命中"无变化"被跳过），是 set-based 的——放进循环里就是
    # N 次全库范围 UPDATE。解析不出名字时**不调用**（那是"待解析"的中间态，
    # 没有名字可写）。
    if preset_written and preset_author_name:
        SQLAlchemyAuthorRepository(session).update_author_name_sync(
            preset_author_id, preset_author_name,
        )

    return results
