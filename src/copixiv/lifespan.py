"""Application singleton graph construction and FastAPI lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from copixiv.config import AppConfig, load_config
from copixiv.db import models
from copixiv.db.engine import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.log import logger
from copixiv.notify.telegram import TelegramNotifier
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.http import PIXIV_IMAGE_HEADERS
from copixiv.query_engine import attach_and_start_engine
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks.manager import TaskManagerSystem

if TYPE_CHECKING:
    from fastapi import FastAPI


def resolve_config(config_source: str | AppConfig | None = None) -> AppConfig:
    """Resolve an :class:`AppConfig` from an instance, YAML path, or defaults."""
    if isinstance(config_source, AppConfig):
        return config_source
    return load_config(config_source)


def load_accounts(
    session_factory: sessionmaker[Session],
    account_pool: AccountPool,
) -> None:
    """Load active Pixiv accounts from the database ``token`` table."""
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


def create_lifespan(
    config: AppConfig,
    *,
    session_factory: sessionmaker[Session] | None = None,
    skip_init_db: bool = False,
    run_background_tasks: bool = True,
):
    """Build the FastAPI lifespan context manager managing all runtime resources."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting copixiv v2...")
        async with AsyncExitStack() as stack:
            owned_engine: Engine | None = None
            sf = session_factory

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

            async def _strip_request_cookies(request: httpx.Request) -> None:
                request.headers.pop("cookie", None)
                http_client.cookies.clear()

            async def _strip_response_cookies(response: httpx.Response) -> None:
                response.cookies.clear()
                http_client.cookies.clear()

            http_client = httpx.AsyncClient(
                proxy=config.proxy.normalized_url(),
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
                max_concurrency=4,
                default_headers=PIXIV_IMAGE_HEADERS,
            )
            image_downloader = NovelAssetPipeline(
                raw_image_downloader,
                epub_builder=epub_builder,
                download_root=file_storage.download_dir,
            )

            account_pool = AccountPool(
                http_client=http_client,
                min_interval=config.pixiv_client.min_interval,
                cooling_duration=config.pixiv_client.cooling_duration,
            )
            await asyncio.to_thread(load_accounts, sf, account_pool)
            stack.push_async_callback(image_downloader.aclose)

            client = PixivClient(
                account_pool=account_pool,
                max_concurrency=config.pixiv_client.max_concurrency,
            )

            notifier = TelegramNotifier.from_config(config, http_client)

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

            app.state.session_factory = sf
            app.state.config = config
            app.state.client = client
            app.state.file_storage = file_storage
            app.state.account_pool = account_pool
            app.state.task_manager = task_manager
            app.state.query_engine = query_engine
            app.state.http_client = http_client
            app.state.engine = owned_engine
            logger.info("Container built successfully.")

            if run_background_tasks:
                await task_manager.start()

            yield
            logger.info("Shutting down copixiv v2...")

    return lifespan
