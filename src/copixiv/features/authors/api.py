"""Author API endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from copixiv.db.tx import read_transaction, run_write_transaction_sync
from copixiv.deps import PixivClientDep, UowDep
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.service import lookup_author_profile


class AuthorLookupResponse(BaseModel):
    """作者 ID 预览（``GET /api/novels/author/{author_id}``）。

    编辑器里填完 ID 就调它，好在保存*之前*告诉用户这个名字对不对；保存路径
    （PATCH）用同一个解析函数，所以预览与实际落库结果一致。

    ``source`` 是诚实性字段：``local`` = 库里已知（离线可用）、``pixiv`` =
    本次现查（说明网络通、作者存在）、``unknown`` = 拿不到名字。它**不影响
    可保存性**——id 才是身份，名字只是缓存，拿不到名字照样允许挂上去。
    """

    author_id: int
    author_name: str | None = None
    source: Literal["local", "pixiv", "unknown"] = "unknown"
    novel_count: int = 0


router = APIRouter()


@router.post("/{author_id}/follow", status_code=204)
def toggle_special_follow(author_id: int, uow: UowDep):
    run_write_transaction_sync(
        uow,
        lambda tx: SQLAlchemyAuthorRepository(tx.session).toggle_special_follow(author_id),
    )


@router.get("/{author_id}", response_model=AuthorLookupResponse)
async def lookup_author(
    author_id: int,
    uow: UowDep,
    client: PixivClientDep,
):
    """作者 ID → 显示名预览（编辑器「作者」一栏的即时反馈）。

    只读、不写库：本地 ``author`` 表命中就返回（零延迟、离线可用），否则问一次
    Pixiv。``source`` 告诉前端这个名字是哪来的；``unknown`` **不是错误**——
    id 才是身份，名字只是缓存，编辑器照样允许保存。保存后名字若为 NULL，那是
    **仓库认可的中间态**、不是"有任务会兜底"：``sync_empty_name`` 默认禁用
    （``scheduled_task.is_enabled`` 列默认 false），会把名字补回来的是**再抓
    一次该作品 / 作者**或编辑器里的「同步」（见 ``update_metadata.py``）。

    放在 ``/author/{author_id}`` 而不是新开 ``/api/authors`` 路由：既有的
    ``POST /author/{author_id}/follow`` 已经在这里，作者相关的两个动作挨着更好找，
    也不用动路由前缀的接线测试。
    """

    profile = await lookup_author_profile(author_id, uow=uow, client=client)
    novel_count = await read_transaction(
        uow,
        lambda tx: SQLAlchemyAuthorRepository(tx.session).count_novels_by_author(author_id),
    )
    return AuthorLookupResponse(
        author_id=profile.author_id,
        author_name=profile.author_name or None,
        source=profile.source,
        novel_count=novel_count,
    )
