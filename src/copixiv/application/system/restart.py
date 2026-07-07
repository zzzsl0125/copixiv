"""Use case: restart system services."""

import subprocess

from copixiv.domain.exceptions import ValidationError
from copixiv.app.logger import logger


class RestartUseCase:
    """Verify sudo credentials and schedule a systemctl restart."""

    def verify_sudo(self, sudo_password: str) -> None:
        """Raise ValidationError if the sudo password is invalid."""
        verify_cmd = ["sudo", "-S", "-v"]
        try:
            result = subprocess.run(
                verify_cmd,
                input=f"{sudo_password}\n",
                text=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                raise ValidationError("密码错误或无 sudo 权限")
        except ValidationError:
            raise
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def execute_restart(sudo_password: str) -> None:
        """Run the systemctl restart commands. Designed for background execution."""
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
