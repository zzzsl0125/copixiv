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
    ``duplicate``：``novel_id`` / ``title``（已在库中那本）；
    ``failed``：``error``（like「无法识别编码…」）。
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
