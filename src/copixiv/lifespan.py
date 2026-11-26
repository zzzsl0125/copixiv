"""Application singleton graph construction and FastAPI lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from copixiv.config import AppConfig, load_config
from copixiv.core.models import SOURCE_PIXIV
from copixiv.db.engine import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.features.novels import field_policy
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.features.sources.service import SourceService
from copixiv.log import logger
from copixiv.notify.telegram import TelegramNotifier
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.query_engine import attach_and_start_engine
from copixiv.sources import (
    LocalSourcePlugin,
    PixivSourcePlugin,
    SourceHttpManager,
    SourceRegistry,
)
from copixiv.sources.esj import EsjSourcePlugin
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.manager import TaskManagerSystem

if TYPE_CHECKING:
    from fastapi import FastAPI


def resolve_config(config_source: str | AppConfig | None = None) -> AppConfig:
    """Resolve an :class:`AppConfig` from an instance, YAML path, or defaults."""
    if isinstance(config_source, AppConfig):
        return config_source
    return load_config(config_source)


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

            proxy_url = config.proxy.normalized_url()
            http_manager = SourceHttpManager(proxy_url)
            stack.push_async_callback(http_manager.aclose)

            shared_http_client = httpx.AsyncClient(
                proxy=proxy_url,
                timeout=30.0,
                limits=httpx.Limits(
                    max_connections=32,
                    max_keepalive_connections=16,
                ),
            )
            stack.push_async_callback(shared_http_client.aclose)

            account_pool = AccountPool(
                http_client=http_manager.get_client(SOURCE_PIXIV),
                min_interval=config.pixiv_client.min_interval,
                cooling_duration=config.pixiv_client.cooling_duration,
            )
            client = PixivClient(
                account_pool=account_pool,
                max_concurrency=config.pixiv_client.max_concurrency,
            )

            source_registry = SourceRegistry(
                [
                    PixivSourcePlugin(
                        http_manager=http_manager,
                        account_pool=account_pool,
                        client=client,
                        default_min_interval=config.pixiv_client.min_interval,
                        default_cooling_duration=config.pixiv_client.cooling_duration,
                        default_max_concurrency=config.pixiv_client.max_concurrency,
                    ),
                    LocalSourcePlugin(),
                    EsjSourcePlugin(http_manager=http_manager),
                ]
            )
            source_service = SourceService(sf, source_registry, http_manager, config)
            await asyncio.to_thread(source_service.bootstrap)
            await http_manager.aclose_retired()

            field_policy.set_source_registry(source_registry)

            file_storage = FileStorage(config.path.download or "download")
            epub_builder = EpubBuilder()
            raw_image_downloader = http_manager.get_image_downloader(SOURCE_PIXIV)
            image_downloader = NovelAssetPipeline(
                raw_image_downloader,
                epub_builder=epub_builder,
                download_root=file_storage.download_dir,
                http_manager=http_manager,
                source_registry=source_registry,
            )
            stack.push_async_callback(image_downloader.aclose)

            notifier = TelegramNotifier.from_config(config, shared_http_client)

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
                source_registry=source_registry,
                source_service=source_service,
                http_manager=http_manager,
            )
            stack.push_async_callback(task_manager.stop)

            app.state.session_factory = sf
            app.state.config = config
            app.state.client = client
            app.state.file_storage = file_storage
            app.state.image_downloader = image_downloader
            app.state.account_pool = account_pool
            app.state.task_manager = task_manager
            app.state.query_engine = query_engine
            app.state.http_client = shared_http_client
            app.state.engine = owned_engine
            app.state.source_registry = source_registry
            app.state.source_service = source_service
            app.state.http_manager = http_manager
            logger.info("Container built successfully.")

            if run_background_tasks:
                await task_manager.start()

            yield
            logger.info("Shutting down copixiv v2...")

    return lifespan
