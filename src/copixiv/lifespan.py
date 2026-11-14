"""Application singleton graph construction and FastAPI lifespan."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from copixiv.config import AppConfig, config, load_config
from copixiv.core.services import QuerySpec
from copixiv.db import models
from copixiv.db.backup import backup_database, cleanup_old_backups
from copixiv.db.engine import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.log import logger
from copixiv.notify.factory import build_notifier
from copixiv.notify.telegram import TelegramNotifier
from copixiv.pixiv.account import AccountStatus, PixivAccount, TokenInfo
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.patch import apply as apply_pixiv_patches
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks.kernel import TaskManagerSystem

if TYPE_CHECKING:
    from fastapi import FastAPI

_BACKUP_TASK_NAME = "backup_database"


@dataclass
class _AppSingletons:
    """The built object graph — bag of singletons returned by :func:`_build`."""

    config: Any
    engine: Engine | None
    session_factory: sessionmaker[Session]
    file_storage: FileStorage
    epub_builder: EpubBuilder
    image_downloader: ImageDownloader
    account_pool: AccountPool
    client: PixivClient
    notifier: TelegramNotifier | None
    task_manager: TaskManagerSystem


def _should_auto_backup(backup_dir: str) -> bool:
    """Return True if this week's backup doesn't already exist in *backup_dir*."""
    this_week = date.today().strftime("%G-W%V")
    week_backup = Path(backup_dir) / f"{this_week}.dump"
    return not week_backup.exists()


def _has_enabled_backup_schedule(session_factory) -> bool:
    """True when a ``backup_database`` cron row is enabled."""
    try:
        with session_factory() as session:
            return session.execute(
                select(models.ScheduledTask.id).where(
                    models.ScheduledTask.task == _BACKUP_TASK_NAME,
                    models.ScheduledTask.is_enabled.is_(True),
                )
            ).first() is not None
    except Exception:
        logger.exception(
            "Could not check for a backup schedule — keeping the startup "
            "backup fallback."
        )
        return False


def _maybe_backup(cfg) -> None:
    """Create a weekly pg_dump backup and remove older ones (keep N most recent)."""
    try:
        backup_database(cfg.database_url, backup_dir=cfg.backup.dir)
        cleanup_old_backups(
            cfg.database_url, keep_count=cfg.backup.keep_count,
            backup_dir=cfg.backup.dir,
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
    """Load Pixiv accounts from the database ``token`` table."""
    try:
        with session_factory() as session:
            tokens = session.execute(
                select(models.Token).where(models.Token.valid == True)
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
        logger.exception("Failed to load accounts from database.")

    logger.warning("No Pixiv accounts loaded — add tokens in Web UI; API calls will fail until then.")


def _build(
    config_source: str | AppConfig | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    skip_init_db: bool = False,
    skip_auto_backup: bool = False,
) -> _AppSingletons:
    """Create and wire all singletons."""
    apply_pixiv_patches()
    logger.info("Pixiv patches applied.")

    if isinstance(config_source, AppConfig):
        cfg = config_source
    elif config_source is None:
        cfg = config
    else:
        cfg = load_config(config_source)

    engine: Engine | None = None
    if session_factory is None:
        engine = create_database_engine(cfg.database_url)
        session_factory = create_session_factory(engine)
        if not skip_init_db:
            init_database(engine, cfg.database_url)
            logger.info("Database initialized (Alembic migrations applied).")

        if (
            not skip_auto_backup
            and _should_auto_backup(cfg.backup.dir)
            and not _has_enabled_backup_schedule(session_factory)
        ):
            logger.info("First startup this week — creating automatic backup.")
            _maybe_backup(cfg)

    file_storage = FileStorage(cfg.path.download or "download")
    epub_builder = EpubBuilder()
    image_downloader = ImageDownloader(
        max_workers=4,
        epub_builder=epub_builder,
        proxy_http=cfg.proxy.url or None,
        proxy_https=cfg.proxy.url or None,
    )

    account_pool = AccountPool(
        proxy_http=cfg.proxy.url or "",
        proxy_https=cfg.proxy.url or "",
        min_interval=cfg.pixiv_client.min_interval,
        cooling_duration=cfg.pixiv_client.cooling_duration,
    )
    _load_accounts(session_factory, account_pool, cfg)
    client = PixivClient(
        account_pool=account_pool,
        max_concurrency=cfg.pixiv_client.max_concurrency,
    )

    notifier = build_notifier(cfg)
    task_manager = TaskManagerSystem(
        session_factory=session_factory,
        client=client,
        file_storage=file_storage,
        image_downloader=image_downloader,
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


def _warm_database(session_factory) -> None:
    """Pre-warm PostgreSQL shared buffers / OS page cache on hot query shapes."""
    try:
        with session_factory() as session:
            repo = SQLAlchemyNovelRepository(session)
            repo.get_novels(QuerySpec(
                order_by="like", per_page=30, min_like=500, min_text=3000,
            ))
            repo.get_novels(QuerySpec(order_by="random", per_page=30))
            repo.get_novels(QuerySpec(
                conditions=[("keyword", "恋")], order_by="like",
                min_like=0, min_text=0,
            ))
            repo.count_novels(QuerySpec(min_like=500, min_text=3000))
        logger.info("数据库预热完成（4 个热查询形态）")
    except Exception:
        logger.exception("数据库预热失败（非致命，继续启动）")


def attach_app_state(app: FastAPI, s: _AppSingletons) -> None:
    """Attach wired singletons onto ``app.state``."""
    app.state.session_factory = s.session_factory
    app.state.config = s.config
    app.state.client = s.client
    app.state.file_storage = s.file_storage
    app.state.image_downloader = s.image_downloader
    app.state.epub_builder = s.epub_builder
    app.state.account_pool = s.account_pool
    app.state.task_manager = s.task_manager


def create_lifespan(s: _AppSingletons, *, run_background_tasks: bool = True):
    """Build the FastAPI lifespan context manager bound to *s*."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting copixiv v2...")
        attach_app_state(app, s)

        try:
            if run_background_tasks:
                total = len(s.account_pool.accounts)
                logger.info(f"Authenticating {total} accounts in parallel...")
                warm_task = asyncio.create_task(
                    asyncio.to_thread(_warm_database, s.session_factory)
                )
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
                await warm_task

                await s.task_manager.start()
            yield
        finally:
            logger.info("Shutting down copixiv v2...")
            await s.task_manager.stop()
            s.image_downloader.shutdown()
            if s.notifier is not None:
                await s.notifier.close()

    return lifespan
