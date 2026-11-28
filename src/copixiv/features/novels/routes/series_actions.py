"""Folded series action endpoints — favourite, metadata edit, sync, delete."""

from fastapi import APIRouter, Depends

from copixiv.db.tx import read_transaction
from copixiv.deps import (
    FileStorageDep,
    ImageDownloaderDep,
    PixivClientDep,
    SourceRegistryDep,
    SourceServiceDep,
    UowDep,
    require_api_key,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.schemas import NovelBase, SeriesUpdateRequest
from copixiv.features.novels.use_cases.series_actions import (
    delete_series as _delete_series,
)
from copixiv.features.novels.use_cases.series_actions import (
    sync_series as _sync_series,
)
from copixiv.features.novels.use_cases.series_actions import (
    toggle_series_favourite as _toggle_series_favourite,
)
from copixiv.features.novels.use_cases.series_actions import (
    update_series_metadata as _update_series_metadata,
)

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/series/{series_id}/favourite", status_code=204)
def toggle_series_favourite(series_id: int, uow: UowDep):
    _toggle_series_favourite(series_id, uow=uow)


@router.patch("/series/{series_id}", response_model=NovelBase)
async def update_series_metadata(
    series_id: int,
    body: SeriesUpdateRequest,
    uow: UowDep,
    client: PixivClientDep,
    source_registry: SourceRegistryDep,
    source_service: SourceServiceDep,
):
    updated = await _update_series_metadata(
        series_id,
        body.model_dump(exclude_unset=True),
        uow=uow,
        client=client,
    )
    await read_transaction(
        uow,
        lambda tx: SQLAlchemyNovelRepository(tx.session).hydrate_links(
            [updated],
            source_service=source_service,
            source_registry=source_registry,
        ),
    )
    return updated


@router.post("/series/{series_id}/sync", response_model=NovelBase)
async def sync_series(
    series_id: int,
    uow: UowDep,
    client: PixivClientDep,
    file_storage: FileStorageDep,
    image_downloader: ImageDownloaderDep,
    source_registry: SourceRegistryDep,
    source_service: SourceServiceDep,
):
    updated = await _sync_series(
        series_id,
        uow=uow,
        client=client,
        download_dir=file_storage.download_dir,
        file_storage=file_storage,
        image_downloader=image_downloader,
        source_registry=source_registry,
        source_service=source_service,
    )
    await read_transaction(
        uow,
        lambda tx: SQLAlchemyNovelRepository(tx.session).hydrate_links(
            [updated],
            source_service=source_service,
            source_registry=source_registry,
        ),
    )
    return updated


@router.delete("/series/{series_id}", status_code=204)
def delete_series(
    series_id: int,
    file_storage: FileStorageDep,
    uow: UowDep,
):
    _delete_series(series_id, uow=uow, file_storage=file_storage)
