"""System API endpoints."""

from fastapi import APIRouter, Depends, Request

from copixiv.domain.services.exclusion import EXCLUDE_BLOCKED_SETTING_KEY
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.deps import get_app_config, get_uow, get_write_uow
from copixiv.web_api.schemas import SystemConfigResponse, SystemConfigUpdate

router = APIRouter()


# Route manifest — mounted automatically by the composition root
# (docs/MODULARITY.md §M9): (prefix, tags) travels with the module.
ROUTE = ("/api/system", ["system"])



async def _config_response(uow: SqlUnitOfWork, app_config) -> dict:
    """Merged config: static values from config.yaml + runtime settings.

    *app_config* is the application config object (injected via ``get_app_config``, docs/MODULARITY.md §M9)
    injected by the endpoint — the module imports nothing from the ``app``
    layer itself (see docs/MODULARITY.md §2.1).

    ``exclude_blocked_tag_novels`` is a runtime setting (default on when
    the settings row is missing), so it can be toggled from the UI.
    """
    return {
        "default_min_like": app_config.frontend.default_min_like,
        "default_min_text": app_config.frontend.default_min_text,
        "batch_download_naming": app_config.batch_download.naming,
        "exclude_blocked_tag_novels": await uow.settings.get_bool(
            EXCLUDE_BLOCKED_SETTING_KEY, default=True
        ),
    }


@router.get("/config", response_model=SystemConfigResponse)
async def get_system_config(
    app_config=Depends(get_app_config),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    return await _config_response(uow, app_config)


@router.put("/config", response_model=SystemConfigResponse)
async def update_system_config(
    data: SystemConfigUpdate,
    app_config=Depends(get_app_config),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if data.exclude_blocked_tag_novels is not None:
        await uow.settings.set_value(
            EXCLUDE_BLOCKED_SETTING_KEY,
            "true" if data.exclude_blocked_tag_novels else "false",
        )
    return await _config_response(uow, app_config)
