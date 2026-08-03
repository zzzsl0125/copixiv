"""System API endpoints."""

from fastapi import APIRouter

from copixiv.web_api.schemas import SystemConfigResponse
from copixiv.application.system import GetConfigUseCase

router = APIRouter()


@router.get("/config", response_model=SystemConfigResponse)
def get_system_config():
    use_case = GetConfigUseCase()
    return use_case.execute()
