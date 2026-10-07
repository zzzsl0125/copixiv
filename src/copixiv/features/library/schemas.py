"""「我的上传」的线上契约（响应形状）—— 前端已按此实现，不可改。

对应 ``frontend/src/types/uploads.ts``：``UploadItemResult`` /
``UploadResponse``。逐条结果三态 ``created | duplicate | failed``；批量上传
部分成功也是 200，无文件是 400（``api.py``）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class UploadResultItem(BaseModel):
    """一条上传结果。

    ``created``：``novel_id`` / ``title`` / ``author_name`` / ``text``；
    ``duplicate``：``novel_id`` / ``title``（已在库中那本）；这次带了预设作者
    （``author_id`` 表单字段）时**也**带 ``author_name`` —— 回显这次写进去的
    名字，前端据此显示「已在库中《…》· 已挂作者：X」；解析不出名字时该字段
    照样不下发（``response_model_exclude_none=True``），前端只显示「已在库中」。
    ``failed``：``error``（like「无法识别编码…」）。

    字段名与可选性与前端 ``frontend/src/types/uploads.ts`` 一一对应，**不可改**
    ——``author_name`` 从"只有 created 才有"扩成"created 与 duplicate 都可能有"
    是唯一的变化，形状本身没动。
    """

    filename: str
    status: Literal["created", "duplicate", "failed"]
    novel_id: int | None = None
    title: str | None = None
    author_name: str | None = None
    text: int | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    results: list[UploadResultItem]
    created: int = 0
    duplicate: int = 0
    failed: int = 0
