"""System API endpoints."""

from fastapi import APIRouter, Depends

from copixiv.app.config import config
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.deps import get_uow, get_write_uow
from copixiv.web_api.schemas import SystemConfigResponse, SystemConfigUpdate

router = APIRouter()

# Settings-table key for the global "exclude blocked-tag novels" toggle.
EXCLUDE_BLOCKED_KEY = "exclude_blocked_tag_novels"


async def _config_response(uow: SqlUnitOfWork) -> dict:
    """Merged config: static values from config.yaml + runtime settings.

    ``exclude_blocked_tag_novels`` is a runtime setting (default on when
    the settings row is missing), so it can be toggled from the UI.
    """
    return {
        "default_min_like": config.frontend.default_min_like,
        "default_min_text": config.frontend.default_min_text,
        "batch_download_naming": config.batch_download.naming,
        "exclude_blocked_tag_novels": await uow.settings.get_bool(
            EXCLUDE_BLOCKED_KEY, default=True
        ),
    }


@router.get("/config", response_model=SystemConfigResponse)
async def get_system_config(uow: SqlUnitOfWork = Depends(get_uow)):
    return await _config_response(uow)


@router.put("/config", response_model=SystemConfigResponse)
async def update_system_config(
    data: SystemConfigUpdate,
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if data.exclude_blocked_tag_novels is not None:
        await uow.settings.set_value(
            EXCLUDE_BLOCKED_KEY,
            "true" if data.exclude_blocked_tag_novels else "false",
        )
    return await _config_response(uow)
