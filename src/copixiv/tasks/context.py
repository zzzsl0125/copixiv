"""TaskContext — the injected dependency channel for task functions.

Dependencies travel exclusively through *ctx* (never through parameter
names), so business argument names can never collide with dependency
names — the two channels are fully separated (docs/MODULARITY.md §M8).
The executor builds the context; business tasks read only what they need.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from copixiv.domain.ports.epub import EpubBuilderPort
from copixiv.domain.ports.notifier import NotifierPort
from copixiv.domain.ports.pixiv import PixivNovelPort
from copixiv.domain.ports.storage import FileStoragePort, ImageDownloaderPort
from copixiv.domain.ports.unit_of_work import UnitOfWork
from copixiv.domain.ports.write_lock import WriteLockPort


@dataclass
class TaskContext:
    """All services a task may need, provided by the task kernel."""

    uow: UnitOfWork | None = None
    session_factory: Any = None  # SQLAlchemy sessionmaker（tasks 层不 import app 层）
    client: PixivNovelPort | None = None
    file_storage: FileStoragePort | None = None
    image_downloader: ImageDownloaderPort | None = None
    epub_builder: EpubBuilderPort | None = None
    config: Any = None  # AppConfig——由组合根装配，类型不跨层引用（§2.1）
    write_lock: WriteLockPort | None = None
    notifier: NotifierPort | None = None
    task_id: int | None = None

    def child_uow(self) -> UnitOfWork:
        """Return a fresh UnitOfWork sharing the process session factory.

        Used by fan-out helpers to give every concurrent branch its own
        session — sessions are never shared across coroutines.
        """
        from copixiv.infrastructure.database.uow import SqlUnitOfWork

        if self.session_factory is None:
            raise RuntimeError(
                "TaskContext.session_factory is not set — the context was "
                "not built by the task kernel."
            )
        return SqlUnitOfWork(self.session_factory)

    def with_uow(self, uow: UnitOfWork) -> "TaskContext":
        """Return a copy of this context with *uow* replaced."""
        return replace(self, uow=uow)
