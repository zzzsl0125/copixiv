"""Single-novel action endpoints — favourite, edit, sync, delete."""

from fastapi import APIRouter, Depends

from copixiv.db.tx import run_write_transaction_sync
from copixiv.deps import (
    FileStorageDep,
    PixivClientDep,
    SourceRegistryDep,
    SourceServiceDep,
    UowDep,
    require_api_key,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.schemas import NovelBase, NovelUpdateRequest
from copixiv.features.novels.use_cases.delete_novel import delete_novel as _delete_novel
from copixiv.features.novels.use_cases.sync_novel import sync_novel as _sync_novel
from copixiv.features.novels.use_cases.update_metadata import (
    update_novel_metadata as _update_novel_metadata,
)

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/{novel_id}/favourite", status_code=204)
def toggle_favourite(novel_id: int, uow: UowDep):
    run_write_transaction_sync(
        uow, lambda tx: SQLAlchemyNovelRepository(tx.session).toggle_favourite(novel_id)
    )


@router.patch("/{novel_id}", response_model=NovelBase)
async def update_novel_metadata(
    novel_id: int,
    body: NovelUpdateRequest,
    uow: UowDep,
    client: PixivClientDep,
):
    """编辑单本小说的元数据（卡片「编辑」按钮）—— docs/NOVEL_EDIT_DESIGN.md §5。

    PATCH 三态：未提交的键不动，显式 ``null`` 清空（``series_index`` /
    ``create_time`` / ``tags`` / ``author_id`` / ``series_id``）。
    只提交**发生变化的字段**是前端的约定，服务端不做 diff（写同样的值也是幂等的）。

    ``title`` / ``author_id`` / ``series_id`` 仅本地书可改
    （2026-09-22 起收掉；清单与
    400 文案的唯一所有者在 ``features/novels/field_policy.py``），pixiv 书提交
    → 400；结构性字段（``source`` / ``path`` / ``id`` / ``has_epub`` …）在 schema
    层就被 ``extra="forbid"`` 拒掉 → 422（``author_name`` / ``series_name``
    自 2026-09-19 起也在其中：它们是 id 的解析结果，不是可编辑值）。响应是更新后的
    **完整**对象，前端据此原地替换卡片（不整页重取）；同一份对象里带
    ``editable_fields``，前端据此置灰输入框。

    pixiv 书那三列错配之后的出口是**同步**（``POST /{novel_id}/sync``）：按 id
    从来源重抓一遍、恢复来源值，而不是靠手改。

    ``author_id`` / ``series_id`` 是仅有的可能触发网络请求的字段（本地表没有
    这个名字时问一次 Pixiv）。失败**不致命**：id 照写，名字留 NULL 待补。
    """
    return await _update_novel_metadata(
        novel_id,
        body.model_dump(exclude_unset=True),
        uow=uow,
        client=client,
    )


@router.post("/{novel_id}/sync", response_model=NovelBase)
async def sync_novel(
    novel_id: int,
    uow: UowDep,
    client: PixivClientDep,
    file_storage: FileStorageDep,
    source_registry: SourceRegistryDep,
    source_service: SourceServiceDep,
):
    """按 id 从来源重抓这一本的元数据并落库（编辑器的「同步」）。

    修的是"手改会与来源打架"那类错配：pixiv 书的 ``title`` / ``author_id`` /
    ``series_id`` 都是只读（``field_policy.py``），错配之后的出口就是这里 ——
    把来源值拉回来（docs/NOVEL_EDIT_DESIGN.md §4.3 的单本版）。

    **无请求体**：动作的输入只有路径上的 id。响应与 PATCH 同形状（更新后的完整
    ``NovelBase``），前端同样原地替换卡片。本地书或未启用来源 → 400；
    pixiv 上已不存在 / 受限（webview 返回空）→ 404。

    依赖用 ``UowDep`` 而**不是** ``get_write_uow``：抓取必须在写事务
    之外，写事务由用例里的 ``run_write_transaction`` 自己开。
    """
    return await _sync_novel(
        novel_id,
        uow=uow,
        client=client,
        download_dir=file_storage.download_dir,
        source_registry=source_registry,
        source_service=source_service,
    )


@router.delete("/{novel_id}", status_code=204)
def delete_novel(
    novel_id: int,
    file_storage: FileStorageDep,
    uow: UowDep,
):
    _delete_novel(novel_id, uow=uow, file_storage=file_storage)
