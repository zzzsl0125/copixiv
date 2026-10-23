"""Pixiv 身份 id 的校验 —— 作者 / 系列共用的一条规则。

``author_id`` 与 ``series_id`` 是**身份**，不是展示值：前端的名字预览、作者页 /
系列页跳转、``author_id:`` / ``series_id:`` 搜索全按它们工作。所以三个写入方
（单本 PATCH、批量编辑、本地书上传的预设作者）必须收同一套规则、报同一套文案
——前端按 ``detail`` 提示，两处一旦漂移行为就分叉。

**为什么在 ``core/``**：这条规则原本住在
``features/novels/update_metadata.py``（先有单本编辑），后来批量编辑
（``features/novels/batch_edit_metadata.py``）与上传切片
（``features/library/api.py``）都 ``import _author_id`` 去用它 —— 两个不同切片
导入另一个切片的**私有**函数，是跨边界的隐式耦合。规则本身是纯校验、无 I/O，
属于领域词汇，没有理由留在某一个切片的内部。
"""

from __future__ import annotations

from typing import Any

from copixiv.core.exceptions import ValidationError


def positive_pixiv_id(raw: Any, label: str, hint: str) -> int:
    """正整数 pixiv id —— ``author_id`` / ``series_id`` 共用的那一条规则。

    为什么必须是 ``int`` 而不是"能转成数字的字符串"：见模块 docstring。
    Pydantic 的 ``int`` 字段已经把 ``"123"`` 收成 ``123``（宽松模式），这里只拦
    下"解析后仍然不是整数"（``true`` / 浮点）与无意义的 0 / 负数。

    上界用 BIGINT：列是 ``BigInteger``，超出会在驱动层报错，不如在这里给 400。
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError(f"{label} ID 必须是整数")
    if raw <= 0:
        raise ValidationError(f"{label} ID 必须是正整数（{hint}）")
    if raw > 2**63 - 1:
        raise ValidationError(f"{label} ID 超出取值范围")
    return raw


def author_id(raw: Any) -> int:
    """正整数 pixiv 作者 id（显式 null 由调用方在更早处处理）。"""
    return positive_pixiv_id(raw, "作者", "pixiv 用户 ID")


def series_id(raw: Any) -> int:
    """正整数 pixiv 系列 id（显式 null 由调用方在更早处处理）。"""
    return positive_pixiv_id(raw, "系列", "pixiv 系列 ID")


__all__ = ["author_id", "positive_pixiv_id", "series_id"]
