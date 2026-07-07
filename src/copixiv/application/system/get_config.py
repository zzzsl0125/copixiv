"""Use case: get system configuration."""

from copixiv.app.config import config


class GetConfigUseCase:
    """Retrieve frontend-facing system configuration values."""

    def execute(self) -> dict:
        return {
            "default_min_like": config.frontend.default_min_like,
            "default_min_text": config.frontend.default_min_text,
        }
