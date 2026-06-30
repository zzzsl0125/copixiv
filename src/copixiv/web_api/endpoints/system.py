"""System API endpoints."""

import subprocess

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, SecretStr

from copixiv.app.config import config
from copixiv.web_api.schemas import SystemConfigResponse

from copixiv.app.logger import logger

router = APIRouter()


@router.get("/config", response_model=SystemConfigResponse)
def get_system_config():
    return SystemConfigResponse(
        default_min_like=config.frontend.default_min_like,
        default_min_text=config.frontend.default_min_text,
    )


class RestartRequest(BaseModel):
    sudo_password: SecretStr


def _execute_restart(sudo_password: str) -> None:
    cmd = [
        "sudo", "-S", "systemctl", "restart",
        "copixiv-frontend.service", "copixiv-backend.service",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=f"{sudo_password}\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.error("Restart failed: {}", result.stderr)
    except Exception as exc:
        logger.error("Restart exception: {}", exc)


@router.post("/restart")
def restart_app(
    request_body: RestartRequest,
    background_tasks: BackgroundTasks,
):
    sudo_password = request_body.sudo_password.get_secret_value()
    verify_cmd = ["sudo", "-S", "-v"]
    try:
        verify_result = subprocess.run(
            verify_cmd,
            input=f"{sudo_password}\n",
            text=True,
            capture_output=True,
            timeout=5,
        )
        if verify_result.returncode != 0:
            raise HTTPException(status_code=403, detail="密码错误或无 sudo 权限")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    background_tasks.add_task(_execute_restart, sudo_password)
    return {"ok": True, "message": "正在重启应用"}
