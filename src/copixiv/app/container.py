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

from copixiv.log import logger
from copixiv.domain.ports.notifier import NotifierPort
from copixiv.infrastructure.notifier.composite import CompositeNotifier
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
        self._notifier: NotifierPort | None = None
        self._task_manager: TaskManagerSystem | None = None

    # ------------------------------------------------------------------
    # Build — wire everything together
    # ------------------------------------------------------------------

    def build(self, backup: bool = False) -> None:
        """Create and wire all singletons.

        Each domain is assembled in its own ``_build_*`` method (a per-domain
        "module manifest" — see docs/MODULARITY.md §M10) so adding a new
        capability extends one function instead of one long method.

        Args:
            backup: If True, create a weekly backup of the database before
                running migrations.  Also performs an automatic weekly
                backup on the first startup of each ISO week.
        """
        apply_pixiv_patches()
        logger.info("Pixiv patches applied.")

        # Resolve database path
        db_path_str = self._resolve_database_path()

        # Auto-backup: first startup of each ISO week
        db_path = Path(db_path_str)
        if backup:
            self._maybe_backup(db_path)
        elif self._should_auto_backup(db_path):
            logger.info("First startup this week — creating automatic backup.")
            self._maybe_backup(db_path)

        # Per-domain assembly
        self._build_database(db_path_str)
        self._build_storage()
        self._build_pixiv()
        self._build_notifier()
        self._build_task_manager()

        logger.info("Container built successfully.")

    # ------------------------------------------------------------------
    # Per-domain assembly ("module manifests")
    # ------------------------------------------------------------------

    def _resolve_database_path(self) -> str:
        """Resolve the configured database path against the working directory."""
        db_path = Path(self.config.path.database)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        return str(db_path)

    def _build_database(self, db_path_str: str) -> None:
        """Engine + migrations + FTS warm-up + hot-cache warm-up."""
        self._engine = create_database_engine(db_path_str)
        self._session_factory = create_session_factory(self._engine)
        init_database(self._engine, db_path_str)
        logger.info("Database initialized (Alembic migrations applied).")

        # FTS warm-up
        FTSManager.warm_up()

        # Database cache warm-up — preload hot index pages into OS cache
        self._warmup_database_cache()

    def _build_storage(self) -> None:
        """File storage + EPUB builder + image downloader.

        All three receive their configuration explicitly (no global-config
        imports — infrastructure must not depend on the app layer).
        """
        self._file_storage = FileStorage(self.config.path.download or "download")
        self._epub_builder = EpubBuilder()
        self._image_downloader = ImageDownloader(
            max_workers=4,
            epub_builder=self._epub_builder,
            proxy_http=self.config.proxy.http,
            proxy_https=self.config.proxy.https,
        )

    def _build_pixiv(self) -> None:
        """Account pool (DB first, token file fallback) + shared client."""
        self._account_pool = AccountPool()
        self._load_accounts()
        self._client = PixivClient(
            account_pool=self._account_pool,
            max_concurrency=self.config.pixiv_client.max_concurrency,
        )

    def _build_notifier(self) -> None:
        """Notification backends — plain config-driven assembly (§M6).

        ``notifiers.enabled`` lists backend names (default ``["telegram"]``;
        empty = notifications off).  ``build_notifiers`` is a plain mapping
        over the two built-in backends — there are no third-party channels,
        so there is no registry (MODULARITY.md §6).
        """
        from copixiv.infrastructure.notifier.factory import build_notifiers

        backends = build_notifiers(self.config)
        self._notifier = CompositeNotifier(backends) if backends else None

    def _build_task_manager(self) -> None:
        """Background scheduler + task executor (see docs/MODULARITY.md §M8)."""
        self._task_manager = TaskManagerSystem(
            session_factory=self._session_factory,
            client=self._client,
            file_storage=self._file_storage,
            image_downloader=self._image_downloader,
            epub_builder=self._epub_builder,
            config=self.config,
            notifier=self._notifier,
        )

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
            or self._client is None or self._task_manager is None
        ):
            raise RuntimeError("Container.build() must be called before create_app()")

        import time as _time
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from contextlib import asynccontextmanager

        from copixiv.web_api.endpoints import (
            novels, tasks, system, tag_preferences, search_history,
            tokens, tag_aliases, failed_novels,
        )
        from copixiv.web_api.middleware import AccessLogMiddleware
        from copixiv.web_api.host_middleware import HostValidationMiddleware
        from copixiv.web_api.api_key_middleware import APIAuthMiddleware
        from copixiv.infrastructure.pixiv.account import AccountStatus

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
            if self._notifier is not None:
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
            allowed_hosts=self.config.security.allowed_hosts,
        )
        app.add_middleware(
            APIAuthMiddleware, api_key=self.config.security.api_key,
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
            allow_origins=self.config.security.allowed_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Routers mount themselves: each endpoint module declares its own
        # ``ROUTE = (prefix, tags)`` manifest (docs/MODULARITY.md §M9), so
        # adding an API area means registering its module here — nothing
        # else in the composition root needs to change.
        for module in (
            novels, tasks, system, tag_preferences, tag_aliases,
            search_history, tokens, failed_novels,
        ):
            prefix, tags = module.ROUTE
            app.include_router(module.router, prefix=prefix, tags=tags)

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

    def _maybe_backup(self, db_path: Path) -> None:
        """Create a weekly backup and remove older ones (keep N most recent)."""
        try:
            backup_database(str(db_path))
            cleanup_old_backups(
                str(db_path), keep_count=self.config.backup.keep_count,
            )
        except Exception:
            logger.exception("Backup failed — continuing without backup.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _warmup_database_cache(self) -> None:
        """Preload database pages into the OS page cache.

        After a server restart — or once memory pressure evicts the ~270 MB
        hot working set (novel like-index + novel_tag index + FTS5 data
        segment) while the server sits idle — the first query that touches
        cold pages pays seconds of *random* disk I/O.  This is I/O wait, not
        CPU: measured ~7000 ms wall vs ~830 ms CPU for a cold first keyword
        search on this 532 MB / 232k-novel DB.  The pain is most acute on
        the first keyword search, which walks several large indexes.

        The old shallow warmup (two small queries touching ~100 rows) did
        almost nothing for this: it left the 94 MB FTS5 index and the bulk
        of the novel indexes cold, and a representative query for one token
        warms only that token's pages (R-18 does not help 「恋」).

        The fix that actually works: a daemon thread **sequentially reads
        the whole DB file once**.  Sequential I/O is 10-50× faster than the
        random reads the queries will later issue, so once the file is
        resident every query is an OS-cache hit.  Verified end-to-end: cold
        first-search 7000 ms → 63 ms after a sequential full-file read.

        It is non-blocking — the server is ready immediately and the read
        races ahead of the user's first query (opening the page and typing a
        search typically takes 3-8 s, by which point the read is done).  If
        a query lands before the read finishes it just pays one cold pass
        — no worse than today — and everything after is warm.  A tiny
        synchronous representative query is kept so the very first request
        still finds a few hot pages; the background read is the real fix.

        ``fadvise(WILLNEED)`` was tried and rejected: the kernel's
        readahead is sequential and does not match B-tree random access, so
        it provided no measurable benefit.
        """
        logger.info("Warming database cache...")
        if self._session_factory is None:
            raise RuntimeError("Container.build() not called")
        try:
            from sqlalchemy import func as _func, select as _select
            from copixiv.infrastructure.database import models as _models
            with self._session_factory() as session:
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
                session.execute(
                    _select(_func.count()).select_from(_models.NovelTag)
                    .where(
                        _models.NovelTag.novel_id.in_(
                            _select(_models.Novel.id).limit(100)
                        )
                    )
                )
        except Exception:
            logger.warning("Database cache warmup (shallow) failed — continuing.")

        # Background sequential full-file read — the real cold-start fix.
        # Pulls every page into the OS page cache without blocking startup.
        import os
        import threading
        import time as _time

        db_path_str = self._resolve_database_path()

        def _seqread(path: str) -> None:
            try:
                t0 = _time.perf_counter()
                size = 0
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)  # 1 MB chunks
                        if not chunk:
                            break
                        size += len(chunk)
                logger.info(
                    f"Database cache warmup (sequential read) complete — "
                    f"{size // (1 << 20)} MB in "
                    f"{_time.perf_counter() - t0:.1f}s."
                )
            except Exception:
                logger.warning(
                    f"Database sequential-read warmup failed for {path} "
                    f"— continuing."
                )

        def _run() -> None:
            # Warm the main DB file first (it holds the indexes that drive
            # query latency); the WAL is small but read it too so committed
            # pages not yet checkpointed are also resident.
            for ext in ("", "-wal"):
                p = db_path_str + ext
                try:
                    if os.path.exists(p) and os.path.getsize(p) > 0:
                        _seqread(p)
                except Exception:
                    logger.warning(f"Sequential-read warmup skipped {p}.")

        threading.Thread(
            target=_run, name="db-cache-warmup", daemon=True,
        ).start()
        logger.info("Database cache warmup scheduled (background sequential read).")

    def _load_accounts(self) -> None:
        """Load Pixiv accounts from the tokens table, falling back to the token file."""
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
                        self._add_account(t.token, t.name, t.premium, t.valid)
                    logger.info(f"Loaded {len(tokens)} Pixiv accounts from database.")
                    return
        except Exception:
            logger.warning("Could not load accounts from database, trying file...")

        # Fallback: load from the token file
        try:
            token_path = Path(self.config.path.token or "pixiv_token.py")
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
                            self._add_account(
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

    def _add_account(
        self, token: str, username: str, premium: bool, valid: bool
    ) -> None:
        """Add a single account to the pool with the shared client settings."""
        self._account_pool.add_account(
            PixivAccount(
                token_info=TokenInfo(
                    token=token,
                    username=username,
                    premium=premium,
                    valid=valid,
                ),
                proxy_http=self.config.proxy.http,
                proxy_https=self.config.proxy.https,
                min_interval=self.config.pixiv_client.min_interval,
                cooling_duration=self.config.pixiv_client.cooling_duration,
            )
        )
