"""Composition root — the single place where all dependencies are wired.

``Container.build()`` creates the full object graph.  Nothing else in the
project imports concrete implementations directly — everything is received
via constructor injection.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from copixiv.app.config import config
from copixiv.infrastructure.database.engine import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from copixiv.infrastructure.database.backup import backup_database, cleanup_old_backups
from copixiv.infrastructure.pixiv.account import PixivAccount, TokenInfo
from copixiv.infrastructure.pixiv.accounts import AccountPool
from copixiv.infrastructure.pixiv.client import PixivClient
from copixiv.infrastructure.pixiv.patch import apply as apply_pixiv_patches
from copixiv.infrastructure.storage.file_storage import FileStorage
from copixiv.infrastructure.storage.image_downloader import ImageDownloader
from copixiv.infrastructure.epub.builder import EpubBuilder
from copixiv.infrastructure.repositories.fts import FTSManager

from copixiv.app.logger import logger
from copixiv.infrastructure.notifier.telegram import TelegramNotifier
from copixiv.tasks.manager import TaskManagerSystem


class Container:
    """Holds all application singletons and provides factories.

    Usage::

        container = Container()
        container.build()              # create all singletons
        container.build(backup=True)   # also create a weekly backup
        app = container.create_app()   # get the FastAPI instance
    """

    def __init__(self, config_path: str | None = None):
        if config_path:
            from copixiv.app.config import load_config
            self.config = load_config(config_path)
        else:
            self.config = config

        # All singletons created by ``build()`` — declared here explicitly
        # so the attribute set is visible to IDEs / type checkers instead
        # of being created dynamically.
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._file_storage: FileStorage | None = None
        self._epub_builder: EpubBuilder | None = None
        self._image_downloader: ImageDownloader | None = None
        self._account_pool: AccountPool | None = None
        self._client: PixivClient | None = None
        self._notifier: TelegramNotifier | None = None
        self._task_manager: TaskManagerSystem | None = None

    # ------------------------------------------------------------------
    # Build — wire everything together
    # ------------------------------------------------------------------

    def build(self, backup: bool = False) -> None:
        """Create and wire all singletons.

        Args:
            backup: If True, create a weekly backup of the database before
                running migrations.  Also performs an automatic weekly
                backup on the first startup of each ISO week.
        """
        apply_pixiv_patches()
        logger.info("Pixiv patches applied.")

        # Resolve database path
        db_path = Path(self.config.path.database)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        db_path_str = str(db_path)

        # Auto-backup: first startup of each ISO week
        if backup:
            self._maybe_backup(db_path)
        elif self._should_auto_backup(db_path):
            logger.info("First startup this week — creating automatic backup.")
            self._maybe_backup(db_path)

        # Database engine + migrations
        self._engine = create_database_engine(db_path_str)
        self._session_factory = create_session_factory(self._engine)
        init_database(self._engine, db_path_str)
        logger.info("Database initialized (Alembic migrations applied).")

        # FTS warm-up
        FTSManager.warm_up()

        # Database cache warm-up — preload hot index pages into OS cache
        self._warmup_database_cache()

        # Storage
        self._file_storage = FileStorage(self.config.path.download or "download")

        # EPUB
        self._epub_builder = EpubBuilder()

        # Image downloader
        self._image_downloader = ImageDownloader(
            max_workers=4, epub_builder=self._epub_builder
        )

        # Pixiv accounts
        self._account_pool = AccountPool()
        self._load_accounts()

        # Pixiv client
        self._client = PixivClient(
            account_pool=self._account_pool,
            max_concurrency=self.config.pixiv_client.max_concurrency,
            min_interval=self.config.pixiv_client.min_interval,
        )

        # Telegram notifier
        self._notifier = TelegramNotifier(self.config)

        # Task manager (background scheduler)
        self._task_manager = TaskManagerSystem(
            session_factory=self._session_factory,
            client=self._client,
            file_storage=self._file_storage,
            image_downloader=self._image_downloader,
            epub_builder=self._epub_builder,
            config=self.config,
            notifier=self._notifier,
        )

        logger.info("Container built successfully.")

    # ------------------------------------------------------------------
    # FastAPI factory
    # ------------------------------------------------------------------

    def create_app(self):
        """Return a fully configured FastAPI application."""
        # build() must have run first (see main.py / README).  Explicit
        # check (not assert) so it also works under ``python -O``; it also
        # narrows the optional types for IDEs / type checkers.
        if (
            self._engine is None or self._session_factory is None
            or self._file_storage is None or self._epub_builder is None
            or self._image_downloader is None or self._account_pool is None
            or self._client is None or self._notifier is None
            or self._task_manager is None
        ):
            raise RuntimeError("Container.build() must be called before create_app()")

        import time as _time
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from contextlib import asynccontextmanager
        from starlette.types import Scope, Receive, Send

        from copixiv.web_api.endpoints import (
            novels, tasks, system, tag_preferences, search_history,
            tokens, tag_aliases,
        )
        from copixiv.infrastructure.pixiv.account import AccountStatus

        # -- pure-ASGI access-log middleware --------------------------------
        class _AccessLogMiddleware:
            """Log every HTTP request to ``access.log`` via loguru."""

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope: Scope, receive: Receive, send: Send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return

                start = _time.monotonic()
                status_code = 0

                async def _send_wrapper(message):
                    nonlocal status_code
                    if message["type"] == "http.response.start":
                        status_code = message.get("status", 0)
                    await send(message)

                try:
                    await self.app(scope, receive, _send_wrapper)
                finally:
                    elapsed_ms = (_time.monotonic() - start) * 1000
                    with logger.contextualize(name="http_access"):
                        logger.info(
                            f"{scope['method']} {scope['path']} → "
                            f"{status_code} {elapsed_ms:.1f}ms",
                        )

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup
            logger.info("Starting copixiv v2...")
            # Store key dependencies on app.state for access by endpoints/tasks
            app.state.session_factory = self._session_factory
            app.state.config = self.config
            app.state.client = self._client
            app.state.file_storage = self._file_storage
            app.state.image_downloader = self._image_downloader
            app.state.epub_builder = self._epub_builder
            app.state.account_pool = self._account_pool
            app.state.task_manager = self._task_manager

            # Pre-authenticate all accounts in parallel so the first API
            # call on each doesn't pay the ~2s auth cost individually.
            total = len(self._account_pool.accounts)
            logger.info(f"Authenticating {total} accounts in parallel...")
            await self._account_pool.authenticate_all()
            active = sum(
                1 for a in self._account_pool.accounts
                if a.status == AccountStatus.ACTIVE
            )
            if active == total:
                logger.info("All {} accounts authenticated.", total)
            else:
                logger.warning(
                    "Account authentication finished: {}/{} active",
                    active, total,
                )

            self._task_manager.start()
            yield
            # Shutdown
            logger.info("Shutting down copixiv v2...")
            self._task_manager.stop()
            self._image_downloader.shutdown()
            await self._notifier.close()

        app = FastAPI(title="Novel Database API", lifespan=lifespan)

        # -- Domain exception handler ------------------------------------------
        from copixiv.domain.exceptions import DomainError
        from fastapi.responses import JSONResponse

        @app.exception_handler(DomainError)
        async def _domain_error_handler(request, exc: DomainError):
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail},
            )

        # Access-log middleware — innermost (added first) so it sees the
        # real status code from the handler, not CORS preflight noise.
        app.add_middleware(_AccessLogMiddleware)

        # allow_origins=["*"] combined with allow_credentials=True is
        # rejected by browsers (wildcard origins cannot carry credentials),
        # and the frontend never sends cookies cross-origin — so drop
        # credentials and let the wildcard be honored.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
        app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
        app.include_router(system.router, prefix="/api/system", tags=["system"])
        app.include_router(
            tag_preferences.router, prefix="/api/tag-preferences", tags=["tag_preferences"]
        )
        app.include_router(
            tag_aliases.router, prefix="/api/tag-aliases", tags=["tag_aliases"]
        )
        app.include_router(
            search_history.router, prefix="/api/search-history", tags=["search_history"]
        )
        app.include_router(tokens.router, prefix="/api/tokens", tags=["tokens"])

        return app

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Release all resources."""
        if self._image_downloader is not None:
            self._image_downloader.shutdown()
        if self._engine is not None:
            self._engine.dispose()

    # ------------------------------------------------------------------
    # Backup helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _should_auto_backup(db_path: Path) -> bool:
        """Return True if this week's backup doesn't already exist."""
        backup_dir = db_path.parent / "backups"
        this_week = date.today().strftime("%G-W%V")
        week_backup = backup_dir / f"{this_week}.db"
        return not week_backup.exists()

    @staticmethod
    def _maybe_backup(db_path: Path) -> None:
        """Create a weekly backup and remove older ones (keep only the latest)."""
        try:
            backup_database(str(db_path))
            cleanup_old_backups(str(db_path))
        except Exception:
            logger.exception("Backup failed — continuing without backup.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _warmup_database_cache(self) -> None:
        """Run representative queries to preload hot index pages into OS cache.

        After a server restart the SQLite page cache and OS buffer cache are
        cold — the first few requests pay the full cost of random disk I/O
        (~20-30 ms per page on HDD).  Touching the key indexes AND the main
        table pages they reference shifts that cost to startup instead of
        the first user request.
        """
        logger.info("Warming database cache...")
        if self._session_factory is None:
            raise RuntimeError("Container.build() not called")
        try:
            from sqlalchemy import func as _func, select as _select
            from copixiv.infrastructure.database import models as _models
            with self._session_factory() as session:
                # Touch the shuffle composite index by scanning through a
                # range of shuffle values.  This also pulls the main-table
                # pages for the rows the index points to (full entity
                # SELECT forces main-table access for columns not in the
                # index).  Built from the ORM so schema renames propagate.
                session.execute(
                    _select(_models.Novel)
                    .where(
                        _models.Novel.shuffle > 0,
                        _models.Novel.like >= 500,
                        _models.Novel.text >= 3000,
                    )
                    .order_by(_models.Novel.shuffle.asc())
                    .limit(100)
                )
                # Touch novel_tag covering index so the first batch tag
                # lookup doesn't stall.
                session.execute(
                    _select(_func.count()).select_from(_models.NovelTag)
                    .where(
                        _models.NovelTag.novel_id.in_(
                            _select(_models.Novel.id).limit(100)
                        )
                    )
                )
            logger.info("Database cache warmup complete.")
        except Exception:
            logger.warning("Database cache warmup failed — continuing.")

    def _load_accounts(self) -> None:
        """Load Pixiv accounts from the tokens table or config."""
        from sqlalchemy import select

        if self._session_factory is None:
            raise RuntimeError("Container.build() not called")

        # Try loading from the database first
        try:
            with self._session_factory() as session:
                from copixiv.infrastructure.database.models import Token as TokenModel
                tokens = session.execute(
                    select(TokenModel).where(TokenModel.valid == True)
                ).scalars().all()

                if tokens:
                    for t in tokens:
                        self._account_pool.add_account(
                            PixivAccount(
                                token_info=TokenInfo(
                                    token=t.token,
                                    username=t.name,
                                    premium=t.premium,
                                    valid=t.valid,
                                ),
                                proxy_http=self.config.proxy.http,
                                proxy_https=self.config.proxy.https,
                                min_interval=self.config.pixiv_client.min_interval,
                                cooling_duration=self.config.pixiv_client.cooling_duration,
                            )
                        )
                    logger.info(f"Loaded {len(tokens)} Pixiv accounts from database.")
                    return
        except Exception:
            logger.warning("Could not load accounts from database, trying file...")

        # Fallback: load from the token file
        try:
            from pathlib import Path
            token_path = Path(self.config.path.token or "pixiv_token.py")
            if token_path and token_path.exists():
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "token_module", token_path
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "TOKENS"):
                        for t_data in mod.TOKENS:
                            self._account_pool.add_account(
                                PixivAccount(
                                    token_info=TokenInfo(**t_data),
                                    proxy_http=self.config.proxy.http,
                                    proxy_https=self.config.proxy.https,
                                    min_interval=self.config.pixiv_client.min_interval,
                                    cooling_duration=self.config.pixiv_client.cooling_duration,
                                )
                            )
                        logger.info(
                            f"Loaded {len(mod.TOKENS)} accounts from token file."
                        )
                        return
        except Exception:
            logger.exception("Failed to load accounts from token file.")

        logger.warning("No Pixiv accounts loaded — API calls will fail.")
