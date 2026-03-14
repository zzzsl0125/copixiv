from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
import subprocess
import time

from core.config import config
from web_api.schemas import SystemConfigResponse


router = APIRouter()

@router.get("/config", response_model=SystemConfigResponse)
def get_config():
    return SystemConfigResponse(
        default_min_like=config.frontend.default_min_like,
        default_min_text=config.frontend.default_min_text,
    )


class RestartRequest(BaseModel):
    password: str

def execute_restart(password: str):
    # wait a bit to ensure the response is sent back
    time.sleep(1)
    
    cmd = ["sudo", "-S", "systemctl", "restart", "copixiv-frontend.service", "copixiv-backend.service"]
    try:
        # pass password via stdin to sudo -S
        result = subprocess.run(
            cmd,
            input=f"{password}\n",
            text=True,
            capture_output=True,
            timeout=10
        )
        if result.returncode != 0:
            import logging
            logging.error(f"Restart failed: {result.stderr}")
    except Exception as e:
        import logging
        logging.error(f"Restart exception: {e}")

@router.post("/restart")
def restart_app(request: RestartRequest, background_tasks: BackgroundTasks):
    # Verify the password works first without actually restarting (e.g. sudo -v)
    verify_cmd = ["sudo", "-S", "-v"]
    try:
        verify_result = subprocess.run(
            verify_cmd,
            input=f"{request.password}\n",
            text=True,
            capture_output=True,
            timeout=5
        )
        if verify_result.returncode != 0:
            raise HTTPException(status_code=403, detail="密码错误或无 sudo 权限")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    background_tasks.add_task(execute_restart, request.password)
    return {"ok": True, "message": "正在重启应用"}
