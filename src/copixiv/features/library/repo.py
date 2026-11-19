"""Local library repository — isolated persistence for locally uploaded books."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from copixiv.core.models import SOURCE_LOCAL, EpubStatus, new_shuffle_key
from copixiv.db import models
from copixiv.db.base import BaseRepository
from copixiv.features.library import local_axis

_ID_LOCK_KEY = "copixiv.library.local_novel_id"


class LocalLibraryRepository(BaseRepository):
    """Repository for local library dedup, coordinate allocation, and inserts."""

    def __init__(self, session: Session):
        super().__init__(session)

    def lock_local_ids(self) -> None:
        """Acquire the transaction-level advisory lock serializing dedup+insert."""
        self.session.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": _ID_LOCK_KEY},
        )

    def next_local_id(self) -> int:
        """Allocate the next monotonic negative coordinate ID."""
        return local_axis.next_local_id(self.session)

    def find_local_duplicate(self, source_key: str) -> tuple[int, str] | None:
        """Return ``(id, title)`` if a local book with *source_key* already exists."""
        row = self.session.execute(
            select(models.Novel.id, models.Novel.title).where(
                models.Novel.source == SOURCE_LOCAL,
                models.Novel.source_key == source_key,
            )
        ).first()
        if row is None:
            return None
        return int(row[0]), str(row[1])

    def get_novel(self, novel_id: int) -> models.Novel | None:
        """Load an existing novel row by primary key."""
        return self.session.get(models.Novel, novel_id)

    def apply_preset_author(
        self,
        novel: models.Novel | None,
        preset_author_id: int,
        preset_author_name: str | None,
    ) -> bool:
        """Attach *preset_author_id*/*preset_author_name* onto an existing duplicate."""
        if novel is None:
            return False
        if novel.author_id == preset_author_id and novel.author_name == preset_author_name:
            return False
        novel.author_id = preset_author_id
        novel.author_name = preset_author_name
        self.session.flush()
        return True

    def mark_epub_done(self, novel: models.Novel) -> None:
        """Record ``has_epub=DONE`` after attaching an EPUB original onto disk."""
        novel.has_epub = EpubStatus.DONE
        self.session.flush()

    def insert_local_novel(
        self,
        *,
        novel_id: int,
        title: str,
        author_id: int | None,
        author_name: str | None,
        path: str,
        text_length: int,
        has_epub: EpubStatus,
        source_key: str,
    ) -> models.Novel:
        """Insert a new local book inside a savepoint so conflicts stay isolated."""
        novel = models.Novel(
            id=novel_id,
            title=title,
            author_id=author_id,
            author_name=author_name,
            path=path,
            like=0,
            view=0,
            text=text_length,
            caption=None,
            series_id=None,
            series_name=None,
            series_index=None,
            create_time=datetime.now(UTC),
            has_epub=has_epub,
            shuffle=new_shuffle_key(),
            tags=[],
            is_favourite=False,
            source=SOURCE_LOCAL,
            source_key=source_key,
        )
        with self.session.begin_nested():
            self.session.add(novel)
            self.session.flush()
        return novel
