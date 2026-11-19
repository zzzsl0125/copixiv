"""copixiv composition root — FastAPI app factory and uvicorn entry point."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from copixiv.error_handlers import register_exception_handlers
from copixiv.lifespan import create_lifespan, resolve_config
from copixiv.log import InterceptHandler, logger, setup_logging
from copixiv.middleware import (
    AccessLogMiddleware,
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
    from fastapi import Depends

    from copixiv.deps import require_api_key
    from copixiv.features.accounts import api as tokens
    from copixiv.features.authors import api as authors
    from copixiv.features.failures import api as failed_novels
    from copixiv.features.library import api as library
    from copixiv.features.novels import routes as novels
    from copixiv.features.search_history import api as search_history
    from copixiv.features.series import api as series
    from copixiv.features.system import api as system
    from copixiv.features.tags import aliases as tag_aliases
    from copixiv.features.tags import preferences as tag_preferences
    from copixiv.tasks import api as tasks

    auth = [Depends(require_api_key)]

    app.include_router(
        authors.router,
        prefix="/api/novels/author",
        tags=["novels"],
        dependencies=auth,
    )
    app.include_router(
        series.router,
        prefix="/api/novels/series",
        tags=["novels"],
        dependencies=auth,
    )
    # Each novels sub-router applies require_api_key or require_download_api_key;
    # mount individually so ``@router.get("")`` gets a non-empty prefix.
    for sub_router in novels.ROUTERS:
        app.include_router(sub_router, prefix="/api/novels", tags=["novels"])
    app.include_router(
        tasks.router,
        prefix="/api/tasks",
        tags=["tasks"],
        dependencies=auth,
    )
    app.include_router(
        system.router,
        prefix="/api/system",
        tags=["system"],
        dependencies=auth,
    )
    app.include_router(
        tag_preferences.router,
        prefix="/api/tag-preferences",
        tags=["tag_preferences"],
        dependencies=auth,
    )
    app.include_router(
        tag_aliases.router,
        prefix="/api/tag-aliases",
        tags=["tag_aliases"],
        dependencies=auth,
    )
    app.include_router(
        search_history.router,
        prefix="/api/search-history",
        tags=["search_history"],
        dependencies=auth,
    )
    app.include_router(
        tokens.router,
        prefix="/api/tokens",
        tags=["tokens"],
        dependencies=auth,
    )
    app.include_router(
        failed_novels.router,
        prefix="/api/failed-novels",
        tags=["failed-novels"],
        dependencies=auth,
    )
    app.include_router(
        library.router,
        prefix="/api/library",
        tags=["library"],
        dependencies=auth,
    )


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built SPA (``frontend/dist``) from the backend."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    root = (dist or _FRONTEND_DIST).resolve()
    index = root / "index.html"
    if not index.is_file():
        logger.warning(
            f"Frontend bundle not found at {root} — API only. Build it with: cd frontend && npm run build"
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
    """Build the FastAPI app; DB/HTTP/Rust singletons start inside lifespan."""
    setup_logging(log_dir)
    resolved_config = resolve_config(config if config is not None else config_path)

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware import Middleware
    from starlette.middleware.gzip import (
        DEFAULT_EXCLUDED_CONTENT_TYPES,
        GZipMiddleware,
    )

    # Order (outermost -> innermost): Host -> AccessLog -> CORS -> GZip
    middlewares = [
        Middleware(
            HostValidationMiddleware,
            allowed_hosts=resolved_config.security.allowed_hosts,
        ),
        Middleware(AccessLogMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=resolved_config.security.allowed_origins or [],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(
            GZipMiddleware,
            minimum_size=1024,
            compresslevel=5,
            exclude_content_types=DEFAULT_EXCLUDED_CONTENT_TYPES
            + (
                "application/epub+zip",
                "application/zip",
            ),
        ),
    ]

    app = FastAPI(
        title="Novel Database API",
        lifespan=create_lifespan(
            resolved_config,
            session_factory=session_factory,
            skip_init_db=skip_init_db,
            skip_auto_backup=skip_auto_backup,
            run_background_tasks=run_background_tasks,
        ),
        middleware=middlewares,
    )
    app.state.config = resolved_config
    register_exception_handlers(app)

    mount_routers(app)
    if mount_spa:
        mount_frontend(app)
    return app


def ensure_port_free(port: int) -> None:
    """Check that *port* is not in use; exit with a clear error if it is."""
    port_re = re.compile(rf":{port}(?!\d)")
    try:
        result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True,
            text=True,
            timeout=5,
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
