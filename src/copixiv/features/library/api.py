"""「我的上传」API —— ``POST /api/library/uploads``（docs/LIBRARY_DESIGN.md §4）。

multipart，字段名 ``files``（可重复，承载多文件）。写端点必须声明
``Depends(get_write_uow)``（``tests/regression/test_m6_write_lock.py`` 执法）。

响应契约（前端 ``frontend/src/types/uploads.ts`` 已按此实现）：

* ``200`` + 逐条结果 —— 部分成功（有的入库、有的重复、有的失败）也是 200；
* ``400`` —— 一个文件都没带。

上传失败**只**出现在响应体里：绝不写 ``failed_novel``（§4-7），
绝不走抓取侧的 ``_upsert_rows``（§4 硬边界）——两条都由测试钉住。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, UploadFile

from copixiv.core.exceptions import ValidationError
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import get_file_storage, get_write_uow
from copixiv.features.library.ingest import (
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    ingest_uploads,
)
from copixiv.features.library.schemas import UploadResponse, UploadResultItem

router = APIRouter()


@router.post(
    "/uploads",
    response_model=UploadResponse,
    # 契约示例里未出现的字段不下发（duplicate 条不带 author_name / text /
    # error …），与前端 ``UploadItemResult`` 的可选字段一一对应。
    response_model_exclude_none=True,
)
async def upload_novels(
    files: list[UploadFile] | None = File(None),
    file_storage=Depends(get_file_storage),
    uow: SqlUnitOfWork = Depends(get_write_uow),
) -> UploadResponse:
    """把本地 TXT 收进库（「我的上传」）。

    整个请求是一个写事务：id 分配 + 指纹去重 + 落库都在里面，
    ``ingest_uploads`` 只 INSERT、逐条给出结果。
    """
    if not files:
        raise ValidationError("请选择要上传的 TXT 文件")

    payloads = [
        (upload.filename or "未命名.txt", await upload.read())
        for upload in files
    ]
    results = ingest_uploads(uow.session, file_storage.download_dir, payloads)

    items = [UploadResultItem(**asdict(result)) for result in results]
    return UploadResponse(
        results=items,
        created=sum(1 for item in items if item.status == STATUS_CREATED),
        duplicate=sum(1 for item in items if item.status == STATUS_DUPLICATE),
        failed=sum(1 for item in items if item.status == STATUS_FAILED),
    )
