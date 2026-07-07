"""System API endpoints."""

from fastapi import APIRouter, BackgroundTasks

from copixiv.web_api.schemas import SystemConfigResponse, RestartRequest
from copixiv.application.system import GetConfigUseCase, RestartUseCase

router = APIRouter()


@router.get("/config", response_model=SystemConfigResponse)
def get_system_config():
    use_case = GetConfigUseCase()
    return use_case.execute()


@router.post("/restart")
def restart_app(
    request_body: RestartRequest,
    background_tasks: BackgroundTasks,
):
    use_case = RestartUseCase()
    sudo_password = request_body.sudo_password.get_secret_value()
    use_case.verify_sudo(sudo_password)
    background_tasks.add_task(use_case.execute_restart, sudo_password)
    return {"ok": True, "message": "正在重启应用"}
