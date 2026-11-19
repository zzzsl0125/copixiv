"""Application singleton graph construction and FastAPI lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from copixiv.config import AppConfig, load_config
from copixiv.db import models
from copixiv.db.backup import backup_database, cleanup_old_backups
from copixiv.db.engine import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.log import logger
from copixiv.notify.factory import build_notifier
from copixiv.notify.telegram import TelegramNotifier
from copixiv.pixiv.account import AccountStatus
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.http import PIXIV_IMAGE_HEADERS, normalize_proxy_url
from copixiv.query_engine import attach_and_start_engine
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks.manager import TaskManagerSystem

if TYPE_CHECKING:
    from fastapi import FastAPI

_BACKUP_TASK_NAME = "backup_database"


@dataclass
class AppSingletons:
    """The built object graph attached onto ``app.state`` during lifespan."""

    config: AppConfig
    engine: Engine | None
    session_factory: sessionmaker[Session]
    file_storage: FileStorage
    epub_builder: EpubBuilder
    image_downloader: NovelAssetPipeline
    account_pool: AccountPool
    client: PixivClient
    notifier: TelegramNotifier | None
    task_manager: TaskManagerSystem
    http_client: httpx.AsyncClient
    needs_startup_backup: bool = False
    query_engine: Any = None
    snapshot_store: PgSnapshotStore | None = None


def resolve_config(config_source: str | AppConfig | None = None) -> AppConfig:
    """Resolve an :class:`AppConfig` from an instance, YAML path, or defaults."""
    if isinstance(config_source, AppConfig):
        return config_source
    return load_config(config_source)


def _should_auto_backup(backup_dir: str) -> bool:
    """Return True if this week's backup doesn't already exist in *backup_dir*."""
    this_week = date.today().strftime("%G-W%V")
    week_backup = Path(backup_dir) / f"{this_week}.dump"
    return not week_backup.exists()


def _has_enabled_backup_schedule(session_factory: sessionmaker[Session]) -> bool:
    """True when a ``backup_database`` cron row is enabled."""
    try:
        with session_factory() as session:
            return (
                session.execute(
                    select(models.ScheduledTask.id).where(
                        models.ScheduledTask.task == _BACKUP_TASK_NAME,
                        models.ScheduledTask.is_enabled.is_(True),
                    )
                ).first()
                is not None
            )
    except Exception:
        logger.exception("Could not check for a backup schedule — keeping the startup backup fallback.")
        return False


def _maybe_backup(cfg: AppConfig) -> None:
    """Create a weekly pg_dump backup and remove older ones."""
    try:
        backup_database(cfg.database_url, backup_dir=cfg.backup.dir)
        cleanup_old_backups(
            cfg.database_url,
            keep_count=cfg.backup.keep_count,
            backup_dir=cfg.backup.dir,
        )
    except Exception:
        logger.exception("Backup failed — continuing without backup.")


def load_accounts(
    session_factory: sessionmaker[Session],
    account_pool: AccountPool,
    cfg: AppConfig | None = None,
) -> None:
    """Load active Pixiv accounts from the database ``token`` table."""
    del cfg
    try:
        with session_factory() as session:
            tokens = session.execute(select(models.Token).where(models.Token.valid == True)).scalars().all()
            if tokens:
                account_pool.reload_from_tokens(
                    [
                        {
                            "name": t.name,
                            "token": t.token,
                            "premium": t.premium,
                            "valid": t.valid,
                            "is_follow": bool(t.is_follow),
                        }
                        for t in tokens
                    ]
                )
                logger.info(f"Loaded {len(tokens)} Pixiv accounts from database.")
                return
    except Exception:
        logger.exception("Failed to load accounts from database.")

    logger.warning("No Pixiv accounts loaded — add tokens in Web UI; API calls will fail until then.")


def attach_app_state(app: FastAPI, s: AppSingletons) -> None:
    """Attach wired singletons onto ``app.state``."""
    app.state.session_factory = s.session_factory
    app.state.config = s.config
    app.state.client = s.client
    app.state.file_storage = s.file_storage
    app.state.image_downloader = s.image_downloader
    app.state.epub_builder = s.epub_builder
    app.state.account_pool = s.account_pool
    app.state.task_manager = s.task_manager
    app.state.query_engine = s.query_engine
    app.state.http_client = s.http_client
    app.state.snapshot_store = s.snapshot_store
    app.state.engine = s.engine


def create_lifespan(
    config: AppConfig,
    *,
    session_factory: sessionmaker[Session] | None = None,
    skip_init_db: bool = False,
    skip_auto_backup: bool = False,
    run_background_tasks: bool = True,
):
    """Build the FastAPI lifespan context manager managing all runtime resources."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting copixiv v2...")
        async with AsyncExitStack() as stack:
            owned_engine: Engine | None = None
            sf = session_factory
            needs_startup_backup = False

            if sf is None:
                owned_engine = create_database_engine(config.database_url)
                stack.callback(owned_engine.dispose)
                sf = create_session_factory(owned_engine)
                if not skip_init_db:
                    await asyncio.to_thread(
                        init_database,
                        owned_engine,
                        config.database_url,
                    )
                    logger.info("Database initialized (Alembic migrations applied).")

                if not skip_auto_backup and _should_auto_backup(config.backup.dir):
                    has_schedule = await asyncio.to_thread(
                        _has_enabled_backup_schedule,
                        sf,
                    )
                    needs_startup_backup = not has_schedule

            async def _strip_request_cookies(request: httpx.Request) -> None:
                request.headers.pop("cookie", None)
                http_client.cookies.clear()

            async def _strip_response_cookies(response: httpx.Response) -> None:
                response.cookies.clear()
                http_client.cookies.clear()

            http_client = httpx.AsyncClient(
                proxy=normalize_proxy_url(config.proxy.url),
                timeout=30.0,
                limits=httpx.Limits(
                    max_connections=32,
                    max_keepalive_connections=16,
                ),
                event_hooks={
                    "request": [_strip_request_cookies],
                    "response": [_strip_response_cookies],
                },
            )
            stack.push_async_callback(http_client.aclose)

            file_storage = FileStorage(config.path.download or "download")
            epub_builder = EpubBuilder()
            raw_image_downloader = ImageDownloader(
                http_client=http_client,
                proxy_url=config.proxy.url or "",
                max_concurrency=4,
                default_headers=PIXIV_IMAGE_HEADERS,
            )
            image_downloader = NovelAssetPipeline(
                raw_image_downloader,
                epub_builder=epub_builder,
                image_headers=PIXIV_IMAGE_HEADERS,
                download_root=file_storage.download_dir,
            )

            account_pool = AccountPool(
                proxy_url=config.proxy.url or "",
                min_interval=config.pixiv_client.min_interval,
                cooling_duration=config.pixiv_client.cooling_duration,
                http_client=http_client,
            )
            await asyncio.to_thread(load_accounts, sf, account_pool, config)
            stack.push_async_callback(account_pool.aclose)
            stack.push_async_callback(image_downloader.aclose)

            client = PixivClient(
                account_pool=account_pool,
                max_concurrency=config.pixiv_client.max_concurrency,
            )

            notifier = build_notifier(config, http_client=http_client)
            if notifier is not None:
                stack.push_async_callback(notifier.close)

            snapshot_store = PgSnapshotStore(sf)
            query_engine = await asyncio.to_thread(
                attach_and_start_engine,
                snapshot_store,
                build_initial=True,
            )
            stack.callback(snapshot_store.detach_runtime)

            task_manager = TaskManagerSystem(
                session_factory=sf,
                client=client,
                file_storage=file_storage,
                image_downloader=image_downloader,
                config=config,
                notifier=notifier,
            )
            stack.push_async_callback(task_manager.stop)

            singletons = AppSingletons(
                config=config,
                engine=owned_engine,
                session_factory=sf,
                file_storage=file_storage,
                epub_builder=epub_builder,
                image_downloader=image_downloader,
                account_pool=account_pool,
                client=client,
                notifier=notifier,
                task_manager=task_manager,
                http_client=http_client,
                needs_startup_backup=needs_startup_backup,
                query_engine=query_engine,
                snapshot_store=snapshot_store,
            )
            attach_app_state(app, singletons)
            logger.info("Container built successfully.")

            backup_task: asyncio.Task[None] | None = None

            async def _wait_startup_backup() -> None:
                if backup_task is not None and not backup_task.done():
                    try:
                        await backup_task
                    except Exception:
                        logger.exception("Background startup backup failed during shutdown.")

            stack.push_async_callback(_wait_startup_backup)

            if run_background_tasks:
                total = len(account_pool.accounts)
                logger.info(f"Authenticating {total} accounts in parallel...")
                await account_pool.authenticate_all()
                active = sum(1 for a in account_pool.accounts if a.status == AccountStatus.ACTIVE)
                if active == total:
                    logger.info("All {} accounts authenticated.", total)
                else:
                    logger.warning(
                        "Account authentication finished: {}/{} active",
                        active,
                        total,
                    )

                await task_manager.start()
                if needs_startup_backup:
                    logger.info("First startup this week — running automatic backup in background.")
                    backup_task = asyncio.create_task(asyncio.to_thread(_maybe_backup, config))

            yield
            logger.info("Shutting down copixiv v2...")

    return lifespan
