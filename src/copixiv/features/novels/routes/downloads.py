"""Browser-navigable file download endpoints (header OR query API key auth)."""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from copixiv.core.exceptions import NotFoundError
from copixiv.core.naming import FormatMode, safe_filename
from copixiv.deps import (
    FileStorageDep,
    TaskManagerDep,
    UowDep,
    require_download_api_key,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.routes.batch import _strip_zip_suffix
from copixiv.features.novels.use_cases.get_novel_file import GetNovelFileUseCase

router = APIRouter(dependencies=[Depends(require_download_api_key)])


@router.get("/export/{task_id}/download")
def download_export_file(
    task_id: int,
    file_storage: FileStorageDep,
    task_manager: TaskManagerDep,
):
    """Stream a completed background export ZIP to the browser."""
    file_path = Path(file_storage.download_dir) / f"batch_export_{task_id}.zip"
    if not file_path.is_file():
        raise NotFoundError("导出文件不存在（可能已被自动清理），请重新导出")

    filename = f"batch_export_{task_id}.zip"
    info = task_manager.get_export_info(task_id)
    if info is not None and info.zip_name:
        stripped = _strip_zip_suffix(info.zip_name)
        if stripped:
            filename = safe_filename(stripped) + ".zip"

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return FileResponse(
        path=str(file_path),
        media_type="application/zip",
        headers=headers,
    )


@router.get("/{novel_id}/download")
def download_novel(
    novel_id: int,
    file_storage: FileStorageDep,
    uow: UowDep,
    format: FormatMode = FormatMode.TXT,
):
    with uow.begin(read_only=True):
        use_case = GetNovelFileUseCase(
            SQLAlchemyNovelRepository(uow.session),
            file_storage.download_dir,
        )
        file_path, media_type = use_case.execute(novel_id, format.value)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_path.name)}"}
    return FileResponse(path=str(file_path), media_type=media_type, headers=headers)
