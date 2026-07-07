"""Composition root — the single place where all dependencies are wired.

``Container.build()`` creates the full object graph.  Nothing else in the
project imports concrete implementations directly — everything is received
via constructor injection.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

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


class Container:
    """Holds all application singletons and provides factories.

    Usage::

        container = Container()
        container.build()              # create all singletons
        container.build(backup=True)   # also create a daily backup
        app = container.create_app()   # get the FastAPI instance
    """

    def __init__(self, config_path: str | None = None):
        if config_path:
            from copixiv.app.config import _load_config
            self.config = _load_config(config_path)
        else:
            self.config = config

    # ------------------------------------------------------------------
    # Build — wire everything together
    # ------------------------------------------------------------------

    def build(self, backup: bool = False) -> None:
        """Create and wire all singletons.

        Args:
            backup: If True, create a daily backup of the database before
                running migrations.  Also performs automatic daily backup
                on first startup of each day.
        """
        apply_pixiv_patches()
        logger.info("Pixiv patches applied.")

        # Resolve database path
        db_path = Path(self.config.path.database)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        db_path_str = str(db_path)

        # Auto-backup: every first startup of the day
        if backup:
            self._maybe_backup(db_path)
        elif self._should_auto_backup(db_path):
            logger.info("First startup today — creating automatic backup.")
            self._maybe_backup(db_path)

        # Database engine + migrations
        self._engine = create_database_engine(db_path_str)
        self._session_factory = create_session_factory(self._engine)
        init_database(self._engine, db_path_str)
        logger.info("Database initialized (Alembic migrations applied).")

        # FTS warm-up
        FTSManager.warm_up()

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
        from copixiv.infrastructure.notifier.telegram import TelegramNotifier
        self._notifier = TelegramNotifier(self.config)

        # Task manager (background scheduler)
        from copixiv.tasks.manager import TaskManagerSystem
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
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from contextlib import asynccontextmanager

        from copixiv.web_api.endpoints import (
            novels, tasks, system, tag_preferences, search_history,
            tokens, tag_aliases,
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
            logger.info(
                "Authenticating %d accounts in parallel...",
                len(self._account_pool.accounts),
            )
            await self._account_pool.authenticate_all()
            logger.info("All accounts authenticated.")

            self._task_manager.start()
            yield
            # Shutdown
            logger.info("Shutting down copixiv v2...")
            self._task_manager.stop()
            self._image_downloader.shutdown()

        app = FastAPI(title="Novel Database API", lifespan=lifespan)

        # -- Domain exception handler ------------------------------------------
        from copixiv.domain.exceptions import DomainError
        from fastapi.responses import JSONResponse

        @app.exception_handler(DomainError)
        async def _domain_error_handler(request, exc: DomainError):
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail},
            )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
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
        self._image_downloader.shutdown()
        if hasattr(self, "_engine"):
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

    def _load_accounts(self) -> None:
        """Load Pixiv accounts from the tokens table or config."""
        from sqlalchemy import select

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
