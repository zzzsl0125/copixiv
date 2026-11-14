"""copixiv composition root — FastAPI app factory and uvicorn entry point."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from copixiv.error_handlers import register_exception_handlers
from copixiv.lifespan import (  # noqa: F401
    _build,
    _load_accounts,
    attach_app_state,
    create_lifespan,
)
from copixiv.log import InterceptHandler, logger, setup_logging
from copixiv.middleware import (
    AccessLogMiddleware,
    APIAuthMiddleware,
    HostValidationMiddleware,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.orm import Session, sessionmaker

    from copixiv.config import AppConfig

UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "loguru": {"()": InterceptHandler},
    },
    "loggers": {
        "uvicorn": {"handlers": ["loguru"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["loguru"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["loguru"], "level": "INFO", "propagate": False},
    },
}

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_ASSET_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}
_INDEX_CACHE = {"Cache-Control": "no-cache"}


def mount_routers(app: FastAPI) -> None:
    """Mount every API router on *app*."""
    from copixiv.features.accounts import api as tokens
    from copixiv.features.failures import api as failed_novels
    from copixiv.features.library import api as library
    from copixiv.features.novels import api as novels
    from copixiv.features.novels import history_api as search_history
    from copixiv.features.system import api as system
    from copixiv.features.tags import aliases as tag_aliases
    from copixiv.features.tags import preferences as tag_preferences
    from copixiv.tasks import api as tasks

    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
    app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(system.router, prefix="/api/system", tags=["system"])
    app.include_router(
        tag_preferences.router, prefix="/api/tag-preferences",
        tags=["tag_preferences"],
    )
    app.include_router(
        tag_aliases.router, prefix="/api/tag-aliases", tags=["tag_aliases"],
    )
    app.include_router(
        search_history.router, prefix="/api/search-history",
        tags=["search_history"],
    )
    app.include_router(tokens.router, prefix="/api/tokens", tags=["tokens"])
    app.include_router(
        failed_novels.router, prefix="/api/failed-novels", tags=["failed-novels"],
    )
    app.include_router(
        library.router, prefix="/api/library", tags=["library"],
    )


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built SPA (``frontend/dist``) from the backend."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    root = (dist or _FRONTEND_DIST).resolve()
    index = root / "index.html"
    if not index.is_file():
        logger.warning(
            f"Frontend bundle not found at {root} — API only. "
            "Build it with: cd frontend && npm run build"
        )
        return False

    assets_root = root / "assets"

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def _frontend_asset(asset_path: str):
        target = (assets_root / asset_path).resolve()
        if not target.is_relative_to(assets_root) or not target.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(target, headers=_ASSET_CACHE)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _frontend_index(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path:
            candidate = (root / full_path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index, headers=_INDEX_CACHE)

    logger.info(f"Serving frontend bundle from {root}")
    return True


def create_app(
    config: str | AppConfig | None = None,
    log_dir: str | None = None,
    *,
    config_path: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    skip_init_db: bool = False,
    skip_auto_backup: bool = False,
    mount_spa: bool = True,
    run_background_tasks: bool = True,
):
    """Build the container and return a fully configured FastAPI app."""
    setup_logging(log_dir)
    resolved_config = config if config is not None else config_path
    s = _build(
        resolved_config,
        session_factory=session_factory,
        skip_init_db=skip_init_db,
        skip_auto_backup=skip_auto_backup,
    )

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware.gzip import (
        DEFAULT_EXCLUDED_CONTENT_TYPES,
        GZipMiddleware,
    )

    app = FastAPI(
        title="Novel Database API",
        lifespan=create_lifespan(s, run_background_tasks=run_background_tasks),
    )
    attach_app_state(app, s)
    register_exception_handlers(app)

    # Order (outermost -> innermost): Host -> API key -> AccessLog -> CORS -> GZip
    app.add_middleware(
        HostValidationMiddleware,
        allowed_hosts=s.config.security.allowed_hosts,
    )
    app.add_middleware(
        APIAuthMiddleware, api_key=s.config.security.api_key,
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.config.security.allowed_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        GZipMiddleware,
        minimum_size=1024,
        compresslevel=5,
        exclude_content_types=DEFAULT_EXCLUDED_CONTENT_TYPES + (
            "application/epub+zip",
            "application/zip",
        ),
    )

    mount_routers(app)
    if mount_spa:
        mount_frontend(app)
    return app


def ensure_port_free(port: int) -> None:
    """Check that *port* is not in use; exit with a clear error if it is."""
    port_re = re.compile(rf":{port}(?!\d)")
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if not port_re.search(line):
                continue
            logger.error(
                f"Port {port} is already in use:\n  {line.strip()}\n"
                "Stop the conflicting process first, or choose another port "
                "via the PORT environment variable."
            )
            sys.exit(1)
    except Exception:
        pass


def main() -> None:
    """Run the uvicorn server (used by the ``copixiv`` console script)."""
    import uvicorn

    setup_logging()
    port = int(os.environ.get("PORT", "9000"))
    ensure_port_free(port)

    try:
        uvicorn.run(
            "copixiv.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=port,
            reload=os.environ.get("COPIXIV_RELOAD") == "1",
            log_config=UVICORN_LOG_CONFIG,
            access_log=False,
        )
    except SystemExit:
        raise
    except Exception:
        logger.exception("uvicorn exited unexpectedly")
        raise


if __name__ == "__main__":
    main()
