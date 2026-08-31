"""copixiv composition root — the single place where all dependencies are wired.

This module merges the old container class, the prior uvicorn entry-point
module, and the three web middlewares / exception mappings into one
root-level module.  Importing this module has no side effects: the object
graph is built only when :func:`create_app` (or the private :func:`_build`)
is called.  Entry paths:

- ``copixiv`` console script → :func:`main` (runs uvicorn with the factory)
- repo-root ``main.py`` shim → ``python main.py`` (the app is built by the
  uvicorn factory exactly once; ``uvicorn main:app`` is no longer supported)
- tests / embedding → ``from copixiv.app import create_app``

Public surface: :func:`create_app`, :func:`main`, :func:`ensure_port_free`,
:data:`UVICORN_LOG_CONFIG`.  The three security/access middlewares and the
``DomainError`` HTTP handler also live here (moved from the former
``web_api`` package).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from copixiv.config import config, load_config
from copixiv.core.exceptions import (
    DomainError,
    NotFoundError,
    TaskAlreadyRunningError,
    ValidationError,
)
from copixiv.db.backup import backup_database, cleanup_old_backups
from copixiv.db.engine import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from copixiv.log import InterceptHandler, logger, setup_logging
from copixiv.notify.composite import CompositeNotifier
from copixiv.notify.factory import build_notifiers
from copixiv.pixiv.account import PixivAccount, TokenInfo
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.patch import apply as apply_pixiv_patches
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks.kernel import TaskManagerSystem

# uvicorn re-applies its own logging config on startup (dictConfig), which
# would wipe the InterceptHandler that setup_logging() installed on the
# "uvicorn" loggers — that's why "Uvicorn running on ..." never reached
# backend.log before.  Pass an explicit config that routes uvicorn's
# loggers back through loguru instead of stderr.
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


# ---------------------------------------------------------------------------
# Domain exception → HTTP status mapping
# ---------------------------------------------------------------------------
#
# Domain exceptions are pure (see ``copixiv.core.exceptions``) and carry no
# HTTP status; the composition root owns the mapping from exception type to
# status.  ``isinstance`` order matters: subclasses are checked first, so
# ``NovelNotFoundError`` (a ``NotFoundError``) naturally maps to 404.
_DOMAIN_HTTP_STATUS = (
    (NotFoundError, 404),
    (ValidationError, 400),
    (TaskAlreadyRunningError, 409),
)


def _domain_error_http_status(exc: DomainError) -> int:
    """Map a domain exception to its HTTP status (default 500)."""
    for exc_type, status in _DOMAIN_HTTP_STATUS:
        if isinstance(exc, exc_type):
            return status
    return 500


# ---------------------------------------------------------------------------
# Middlewares (moved verbatim from the former web_api package)
# ---------------------------------------------------------------------------
#
# Pure-ASGI middlewares (no ``BaseHTTPMiddleware``) so they see the real
# response status code from exception handlers and keep ``log/access.log``
# in sync with actual responses.

import ipaddress
import json
import time

from starlette.types import Receive, Scope, Send


class AccessLogMiddleware:
    """Log every HTTP request to ``access.log`` via loguru."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        status_code = 0

        async def _send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            with logger.contextualize(name="http_access"):
                logger.info(
                    f"{scope['method']} {scope['path']} → "
                    f"{status_code} {elapsed_ms:.1f}ms",
                )


def _extract_host_header(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"host":
            return value.decode("latin-1")
    return ""


def _normalize_host(raw: str) -> str:
    """Strip the port (and IPv6 brackets) from a Host header value."""
    host = raw.strip()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[1:end]
    elif host.count(":") == 1:
        # "example.com:8000" / "127.0.0.1:8000" → strip the port
        host = host.rsplit(":", 1)[0]
    # Otherwise: bare IPv6 literal ("::1") with multiple colons and no port
    # — keep it whole so ipaddress can parse it.
    return host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HostValidationMiddleware:
    """Reject requests whose ``Host`` header is not an allow-listed value.

    Pass-through conditions (any one suffices):
    - the host is an IP literal (IPv4/IPv6, brackets already stripped)
    - ``host.lower()`` is ``localhost`` or is listed in *allowed_hosts*

    Tests reach this middleware through the real configuration: their app
    config declares the host they use (e.g. ``allowed_hosts: [testserver]``
    for ``TestClient``'s default Host header) — there is no special case
    for test hosts in production code.
    """

    def __init__(self, app, allowed_hosts: list[str] | None = None):
        self.app = app
        self.allowed_hosts = allowed_hosts or []

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = _normalize_host(_extract_host_header(scope))
        if self._is_allowed(host):
            await self.app(scope, receive, send)
            return

        await self._send_error(send, 400, "Invalid Host header")

    def _is_allowed(self, host: str) -> bool:
        lowered = host.lower()
        if _is_ip_literal(host):
            return True
        if lowered == "localhost":
            return True
        allowed = {h.lower() for h in self.allowed_hosts}
        return lowered in allowed

    @staticmethod
    async def _send_error(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class APIAuthMiddleware:
    """Require ``X-API-Key`` on ``/api/`` requests when a key is configured."""

    def __init__(self, app, api_key: str = ""):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if not self.api_key:
            await self.app(scope, receive, send)
            return

        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and scope["method"] != "OPTIONS"
        ):
            if self._extract_api_key(scope) != self.api_key:
                await self._send_error(send, 401, "Invalid or missing API key")
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _extract_api_key(scope: Scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-api-key":
                return value.decode("latin-1")

        # Fallback: accept the key via ?api_key= so the frontend can drive a
        # normal browser navigation to a download URL (some in-app browsers /
        # WebViews can't handle blob: object-URL downloads, so the client
        # navigates straight to the endpoint instead of fetching a Blob).
        # The key is already shipped in the client bundle, so a query-param
        # form is not an additional leak vs. the X-API-Key header.
        query = scope.get("query_string", b"")
        if query:
            from urllib.parse import parse_qs
            params = parse_qs(query.decode("latin-1"))
            values = params.get("api_key")
            if values:
                return values[0]
        return None

    @staticmethod
    async def _send_error(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Wired singletons
# ---------------------------------------------------------------------------


@dataclass
class _AppSingletons:
    """The built object graph — bag of singletons returned by :func:`_build`."""

    config: Any
    engine: Engine
    session_factory: sessionmaker[Session]
    file_storage: FileStorage
    epub_builder: EpubBuilder
    image_downloader: ImageDownloader
    account_pool: AccountPool
    client: PixivClient
    notifier: CompositeNotifier | None
    task_manager: TaskManagerSystem


# ---------------------------------------------------------------------------
# Build — wire everything together
# ---------------------------------------------------------------------------

# Weekly ``pg_dump`` backups land in this directory (project-relative).
_BACKUP_DIR = "backups"


def _should_auto_backup() -> bool:
    """Return True if this week's backup doesn't already exist."""
    this_week = date.today().strftime("%G-W%V")
    week_backup = Path(_BACKUP_DIR) / f"{this_week}.dump"
    return not week_backup.exists()


def _maybe_backup(cfg) -> None:
    """Create a weekly pg_dump backup and remove older ones (keep N most recent)."""
    try:
        backup_database(cfg.database_url, backup_dir=_BACKUP_DIR)
        cleanup_old_backups(
            cfg.database_url, keep_count=cfg.backup.keep_count,
            backup_dir=_BACKUP_DIR,
        )
    except Exception:
        logger.exception("Backup failed — continuing without backup.")


def _add_account(
    account_pool: AccountPool, cfg, token: str, username: str,
    premium: bool, valid: bool, follow: bool = False,
) -> None:
    """Add a single account to the pool with the shared client settings."""
    account_pool.add_account(
        PixivAccount(
            token_info=TokenInfo(
                token=token,
                username=username,
                premium=premium,
                valid=valid,
                follow=follow,
            ),
            proxy_http=cfg.proxy.url or None,
            proxy_https=cfg.proxy.url or None,
            min_interval=cfg.pixiv_client.min_interval,
            cooling_duration=cfg.pixiv_client.cooling_duration,
        )
    )


def _load_accounts(
    session_factory: sessionmaker[Session], account_pool: AccountPool, cfg,
) -> None:
    """Load Pixiv accounts from the tokens table, falling back to the token file."""
    from sqlalchemy import select

    # Try loading from the database first
    try:
        with session_factory() as session:
            from copixiv.db.models import Token as TokenModel
            tokens = session.execute(
                select(TokenModel).where(TokenModel.valid == True)
            ).scalars().all()

            if tokens:
                for t in tokens:
                    _add_account(
                        account_pool, cfg,
                        t.token, t.name, t.premium, t.valid, t.is_follow,
                    )
                logger.info(f"Loaded {len(tokens)} Pixiv accounts from database.")
                return
    except Exception:
        logger.warning("Could not load accounts from database, trying file...")

    # Fallback: load from the token file
    try:
        token_path = Path(cfg.path.token or "pixiv_token.py")
        if token_path.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "token_module", token_path
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "TOKENS"):
                    for t_data in mod.TOKENS:
                        _add_account(
                            account_pool, cfg,
                            t_data.get("token", ""),
                            t_data.get("username", ""),
                            t_data.get("premium", False),
                            t_data.get("valid", True),
                        )
                    logger.info(
                        f"Loaded {len(mod.TOKENS)} accounts from token file."
                    )
                    return
    except Exception:
        logger.exception("Failed to load accounts from token file.")

    logger.warning("No Pixiv accounts loaded — API calls will fail.")


def _build(config_path: str | None = None) -> _AppSingletons:
    """Create and wire all singletons (the old ``Container.build()``).

    This is the private assembly step used by :func:`create_app` and by the
    ``scripts/test_ingest.py`` dev harness.  Public entry points should go
    through :func:`create_app`.
    """
    apply_pixiv_patches()
    logger.info("Pixiv patches applied.")

    cfg = config if config_path is None else load_config(config_path)

    # Auto-backup: first startup of each ISO week (pg_dump — driven by
    # ``cfg.database_url``, not a file path).
    if _should_auto_backup():
        logger.info("First startup this week — creating automatic backup.")
        _maybe_backup(cfg)

    engine = create_database_engine(cfg.database_url)
    session_factory = create_session_factory(engine)
    init_database(engine, cfg.database_url)
    logger.info("Database initialized (Alembic migrations applied).")

    # File storage + EPUB builder + image downloader
    file_storage = FileStorage(cfg.path.download or "download")
    epub_builder = EpubBuilder()
    image_downloader = ImageDownloader(
        max_workers=4,
        epub_builder=epub_builder,
        proxy_http=cfg.proxy.url or None,
        proxy_https=cfg.proxy.url or None,
    )

    # Account pool (DB first, token file fallback) + shared client
    account_pool = AccountPool()
    _load_accounts(session_factory, account_pool, cfg)
    client = PixivClient(
        account_pool=account_pool,
        max_concurrency=cfg.pixiv_client.max_concurrency,
    )

    # Notification backends — plain config-driven assembly
    backends = build_notifiers(cfg)
    notifier = CompositeNotifier(backends) if backends else None

    # Background scheduler + task executor
    task_manager = TaskManagerSystem(
        session_factory=session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
        epub_builder=epub_builder,
        config=cfg,
        notifier=notifier,
    )

    logger.info("Container built successfully.")

    return _AppSingletons(
        config=cfg,
        engine=engine,
        session_factory=session_factory,
        file_storage=file_storage,
        epub_builder=epub_builder,
        image_downloader=image_downloader,
        account_pool=account_pool,
        client=client,
        notifier=notifier,
        task_manager=task_manager,
    )


# ---------------------------------------------------------------------------
# FastAPI factory
# ---------------------------------------------------------------------------


def create_app(config_path: str | None = None):
    """Build the container and return a fully configured FastAPI app.

    ``config_path`` selects an alternate config.yaml (defaults to the
    shared application config).  Construction — logging setup, DB
    migrations, account loading — happens here, never at import time.
    """
    # Configure logging before anything else so all log output is captured.
    setup_logging()

    s = _build(config_path)

    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    from copixiv.tasks import api as tasks
    from copixiv.features.novels import api as novels
    from copixiv.features.novels import history_api as search_history
    from copixiv.features.accounts import api as tokens
    from copixiv.features.tags import aliases as tag_aliases
    from copixiv.features.tags import preferences as tag_preferences
    from copixiv.features.failures import api as failed_novels
    from copixiv.features.system import api as system
    from copixiv.pixiv.account import AccountStatus

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Starting copixiv v2...")
        # Store key dependencies on app.state for access by endpoints/tasks.
        app.state.session_factory = s.session_factory
        app.state.config = s.config
        app.state.client = s.client
        app.state.file_storage = s.file_storage
        app.state.image_downloader = s.image_downloader
        app.state.epub_builder = s.epub_builder
        app.state.account_pool = s.account_pool
        app.state.task_manager = s.task_manager

        try:
            # Pre-authenticate all accounts in parallel so the first API
            # call on each doesn't pay the ~2s auth cost individually.
            total = len(s.account_pool.accounts)
            logger.info(f"Authenticating {total} accounts in parallel...")
            await s.account_pool.authenticate_all()
            active = sum(
                1 for a in s.account_pool.accounts
                if a.status == AccountStatus.ACTIVE
            )
            if active == total:
                logger.info("All {} accounts authenticated.", total)
            else:
                logger.warning(
                    "Account authentication finished: {}/{} active",
                    active, total,
                )

            s.task_manager.start()
            yield
        finally:
            # Shutdown
            logger.info("Shutting down copixiv v2...")
            s.task_manager.stop()
            s.image_downloader.shutdown()
            if s.notifier is not None:
                await s.notifier.close()

    app = FastAPI(title="Novel Database API", lifespan=lifespan)

    # -- Domain exception handler ------------------------------------------
    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    # ------------------------------------------------------------------
    # Security middlewares
    #
    # The server still binds 0.0.0.0 (see main.py) so it stays reachable
    # from the LAN — that is an intentional feature and must not change.
    # Because the socket is open to the whole network, the trust boundary
    # is enforced here in the middleware stack instead:
    #   - Host validation blocks DNS-rebinding (a malicious page resolving
    #     its own domain to this server and reading /api/tokens).
    #   - An optional shared API key gates every /api/ call.
    #   - CORS is restricted to a small origin whitelist.
    #
    # Starlette's add_middleware inserts at index 0, so the first one
    # added ends up outermost.  Order: Host → api_key → AccessLog → CORS.
    # Host wraps everything so a rejected Host never reaches any router.
    app.add_middleware(
        HostValidationMiddleware,
        allowed_hosts=s.config.security.allowed_hosts,
    )
    app.add_middleware(
        APIAuthMiddleware, api_key=s.config.security.api_key,
    )

    # Access-log middleware — added after the security middlewares (so it
    # sits inside them) but before CORS, so it still sees the real status
    # code from the handler, not CORS preflight noise.
    app.add_middleware(AccessLogMiddleware)

    # CORS — innermost of the security layers.  Whitelist replaces the old
    # wildcard: allow_credentials stays False (the frontend never sends
    # cookies cross-origin), and allow_methods/headers stay permissive.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.config.security.allowed_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers are mounted explicitly here, one ``include_router`` per API
    # area.  Each endpoint module owns its ``APIRouter``; the prefix and
    # tags travel with the registration call below.  Mount order matches
    # the former per-module prefix manifest (novels → tasks → system →
    # tag_preferences → tag_aliases → search_history → tokens →
    # failed_novels).
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

    return app


# ---------------------------------------------------------------------------
# uvicorn launcher
# ---------------------------------------------------------------------------


def ensure_port_free(port: int) -> None:
    """Check that *port* is not in use; exit with a clear error if it is.

    Replaces the old ``kill_port`` behaviour (which force-killed whatever
    process was listening on the port) — killing unrelated processes is
    dangerous, so we now fail fast with actionable information instead.

    The message goes through loguru (not stderr) so it lands in
    ``log/backend.log`` as well as the journal — stderr-only messages
    are invisible in the file logs and make restart loops hard to read.
    """
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" not in line:
                continue
            logger.error(
                f"Port {port} is already in use:\n  {line.strip()}\n"
                "Stop the conflicting process first, or choose another port "
                "via the PORT environment variable."
            )
            sys.exit(1)
    except Exception:
        # ss unavailable — let uvicorn report the bind failure instead.
        pass


def main() -> None:
    """Run the uvicorn server (used by the ``copixiv`` console script).

    ``reload`` is opt-in via ``COPIXIV_RELOAD=1`` — it is a development
    convenience only.  Under systemd the reloader is actively harmful:
    with watchfiles absent uvicorn falls back to StatReload, which polls
    every ``*.py`` under the working directory (including the whole
    ``.venv`` tree) and turns any transient file touch into a restart
    storm that fights the unit's own ``Restart=`` policy.  Deployments
    must run without it; use ``systemctl restart`` instead.
    """
    import uvicorn

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
        # Surface unexpected uvicorn exits through loguru so the reason
        # reaches log/backend.log as well as the journal.
        logger.exception("uvicorn exited unexpectedly")
        raise


if __name__ == "__main__":
    main()
