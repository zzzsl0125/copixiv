"""「我的上传」API —— ``POST /api/library/uploads``（docs/LIBRARY_DESIGN.md §4）。

multipart，字段名 ``files``（可重复，承载多文件）；可选表单字段 ``author_id``
（**整批共用**：一次上传的这批书挂同一位作者）。

收 **TXT 与 EPUB** 两种本地文件（同一批可以混着来）：分派与解析在
``ingest.py`` / ``storage.epub.reader``，本端点对格式无感知——
多文件时按文件逐个分派，TXT 走解码管线、EPUB 走抽取管线（§4.1）。

**写事务纪律（本端点的形状由它决定）：名字解析必须在写事务之前完成。**
``ingest_uploads`` 一进去就拿事务级咨询锁（``pg_advisory_xact_lock``），而作者名
解析可能要问一次 pixiv（``lookup_author_profile``：本地表优先、pixiv 兜底）。
如果把网络调用留在写事务里，等于拿着咨询锁 + 一个开着的事务等外部 HTTP。
所以这里用单个 ``uow: UowDep`` 明确分两阶段执行：

1. 若提供了 ``author_id``，先在写事务外调用 ``lookup_author_profile``
   解析一次名字（整批共用，本地表命中时零网络，只在未命中时请求 pixiv）；
2. 再通过 ``run_write_transaction(uow, ...)`` 在独立写事务中调用
   ``ingest_uploads`` 落库（写事务内无网络调用）。

响应契约（前端 ``frontend/src/types/uploads.ts`` 已按此实现）：

* ``200`` + 逐条结果 —— 部分成功（有的入库、有的重复、有的失败）也是 200；
* ``400`` —— 一个文件都没带；单个文件超过 :data:`MAX_UPLOAD_BYTES` 或整批
  总大小超过 :data:`MAX_BATCH_UPLOAD_BYTES`；``author_id`` 不是正整数（0 / 负数）；
* ``422`` —— ``author_id`` 不是整数（``"abc"``，FastAPI 类型转换阶段）。

**体积上限在这一层拦**（:func:`_read_capped`）：``ingest`` 拿到的已经是完整
bytes，而 ``storage.epub.reader`` 的容器上限只对「按 EPUB 处理」的文件生效 ——
一个超大 ``.txt`` 会先被整个读进内存再解码。上传是同步请求，它不该有能力把进程
的内存打满。

上传失败**只**出现在响应体里：绝不写 ``failed_novel``（§4-7），绝不走抓取侧的
``_upsert_rows``（§4 硬边界）——两条都由测试钉住。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import author_id as _author_id
from copixiv.db.tx import run_write_transaction
from copixiv.deps import (
    FileStorageDep,
    PixivClientDep,
    UowDep,
)
from copixiv.features.authors.service import lookup_author_profile
from copixiv.features.library.ingest import (
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    ingest_uploads,
)


class UploadResultItem(BaseModel):
    """一条上传结果。

    ``created``：``novel_id`` / ``title`` / ``author_name`` / ``text``；
    ``duplicate``：``novel_id`` / ``title``（已在库中那本）；这次带了预设作者
    （``author_id`` 表单字段）时**也**带 ``author_name`` —— 回显这次写进去的
    名字，前端据此显示「已在库中《…》· 已挂作者：X」；解析不出名字时该字段
    照样不下发（``response_model_exclude_none=True``），前端只显示「已在库中」。
    ``duplicate`` + ``epub_attached=True``：这次上传的 EPUB 原件被补挂到了那本
    已在库、此前没有 EPUB 的本地书上（§4.1）——前端据此刷新列表 / 显示补挂。
    ``failed``：``error``（like「无法识别编码…」）。

    字段名与可选性与前端 ``frontend/src/types/uploads.ts`` 一一对应，**不可改**
    ——``author_name`` 从"只有 created 才有"扩成"created 与 duplicate 都可能有"、
    ``epub_attached`` 是新增的**可选**字段（只有 true 时才下发），两次都是
    向后兼容的扩展，形状本身没动。
    """

    filename: str
    status: Literal["created", "duplicate", "failed"]
    novel_id: int | None = None
    title: str | None = None
    author_name: str | None = None
    text: int | None = None
    error: str | None = None
    epub_attached: bool | None = None


class UploadResponse(BaseModel):
    results: list[UploadResultItem]
    created: int = 0
    duplicate: int = 0
    failed: int = 0


router = APIRouter()


# 单个文件的上限。**这一层必须自己拦**：``ingest`` 拿到的已经是完整 bytes，
# 而 ``epub.reader`` 的容器上限只在「按 EPUB 处理」时才生效 —— 一个 2 GB 的
# ``.txt`` 会先被整个读进内存、再整个解码、再被判失败。上传是同步请求，它不该
# 有能力把进程的内存打满。
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
UPLOAD_TOO_LARGE_ERROR = "文件过大（单个文件最多 128 MB）"
MAX_BATCH_UPLOAD_BYTES = 512 * 1024 * 1024
BATCH_TOO_LARGE_ERROR = "单批上传总大小过大（单批最多 512 MB）"


async def _read_capped(upload: UploadFile) -> bytes:
    """读一份上传并卡上限 → bytes；超限抛 :class:`ValidationError`（400）。

    读 *MAX_UPLOAD_BYTES + 1* 字节再判长度，而不是先读全部再看大小：
    ``UploadFile`` 已经在 Starlette 里落成临时文件（大文件不会全量进内存），
    但我们自己那一次 ``read()`` 会 —— 多读一个字节就够判断，省下的是这之后的
    全部内存。超限的请求整批 400（与「一个文件都没带」同一口径）：半批入库
    半批拒收会让"我到底收进去了几本"变成猜谜。
    """
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(UPLOAD_TOO_LARGE_ERROR)
    return data


@router.post(
    "/uploads",
    response_model=UploadResponse,
    # 契约示例里未出现的字段不下发（duplicate 条不带 text / error，
    # 解析不出作者名时也不带 author_name），与前端 ``UploadItemResult``
    # 的可选字段一一对应。
    response_model_exclude_none=True,
)
async def upload_novels(
    file_storage: FileStorageDep,
    uow: UowDep,
    client: PixivClientDep,
    files: Annotated[list[UploadFile] | None, File()] = None,
    # ``int | None``：multipart 送来的是字符串，由 FastAPI 转换 —— ``"abc"``
    # 在这一步就是 422（类型不是业务规则）；``0`` / ``-5`` 能转成 int，落到下面
    # ``_author_id`` 手里才是 400。字段不出现 = 不带预设，与旧行为逐字一致；
    # 空串（前端表单可能原样带上）由这一层的宽松转换当成"没给"（实测 200 +
    # 旧行为），不是我们写的规则，所以别在这里加校验去改它的形态。
    author_id: Annotated[int | None, Form()] = None,
) -> UploadResponse:
    """把本地 TXT / EPUB 收进库（「我的上传」）。

    整个写入是一个写事务：id 分配 + 指纹去重 + 落库都在里面，
    ``ingest_uploads`` 只写库、逐条给出结果。每个文件按后缀 / 内容分派：
    TXT 解码入库、EPUB 抽正文 + 存原件（§4.1）。

    ``author_id`` 可选、**整批共用**：给了就把这批书挂到这位 pixiv 作者名下
    （新书直接写 id；已在库的重复件也应用——用户拍板），名字由 id 解析出来
    （本地 ``author`` 表优先、pixiv 兜底、**整批只解析一次**），解析不出就写
    ``NULL``（"待解析"，仓库认可的中间态；``sync_empty_name`` 默认禁用，别把它
    当兜底任务）。**本版刻意不做系列**（``series_id``）：系列要连名字与章节号
    一起收尾（``update_metadata.py::_series_columns``），是另一件事。

    **解析在写事务之外**（模块 docstring 第 1 条）：防的是"写事务里等 pixiv"
    ——咨询锁与一个开着的事务被慢对端攥着。失败不致命：名字是缓存，id 是身份。
    """
    if not files:
        raise ValidationError("请选择要上传的 TXT / EPUB 文件")

    payloads: list[tuple[str, bytes]] = []
    total_bytes = 0
    for upload in files:
        data = await _read_capped(upload)
        total_bytes += len(data)
        if total_bytes > MAX_BATCH_UPLOAD_BYTES:
            raise ValidationError(BATCH_TOO_LARGE_ERROR)
        payloads.append((upload.filename or "未命名.txt", data))

    preset_author_id: int | None = None
    preset_author_name: str | None = None
    if author_id is not None:
        # 0 / 负数 / 布尔 → 400，文案来自 ``_author_id``（唯一所有者）。
        preset_author_id = _author_id(author_id)

        profile = await lookup_author_profile(
            preset_author_id,
            uow=uow,
            client=client,
        )
        preset_author_name = profile.author_name

    # ---- 落库：写事务（咨询锁在里面），无网络 ----
    results = await run_write_transaction(
        uow,
        lambda tx: ingest_uploads(
            tx.session,
            file_storage,
            payloads,
            preset_author_id=preset_author_id,
            preset_author_name=preset_author_name,
        ),
    )

    items = [UploadResultItem(**asdict(result)) for result in results]
    return UploadResponse(
        results=items,
        created=sum(1 for item in items if item.status == STATUS_CREATED),
        duplicate=sum(1 for item in items if item.status == STATUS_DUPLICATE),
        failed=sum(1 for item in items if item.status == STATUS_FAILED),
    )
