"""System API endpoints."""

from fastapi import APIRouter

from copixiv.app.config import config
from copixiv.web_api.schemas import SystemConfigResponse

router = APIRouter()


@router.get("/config", response_model=SystemConfigResponse)
def get_system_config():
    return SystemConfigResponse(
        default_min_like=config.frontend.default_min_like,
        default_min_text=config.frontend.default_min_text,
    )
