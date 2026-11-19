"""Tag entity repository."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import delete as _delete
from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository


class TagRepository(BaseRepository):
    """Repository for tag rows and orphan tag pruning."""

    def get_all(self) -> Sequence[Any]:
        return list(self.session.execute(select(models.Tag).order_by(models.Tag.name)).scalars().all())

    def get_or_create_tag_id(self, name: str) -> int:
        """Return ``tag.id`` for *name*, inserting a row if missing."""
        tag = self.session.execute(select(models.Tag).where(models.Tag.name == name)).scalar_one_or_none()
        if tag:
            return tag.id
        self.session.execute(
            pg_insert(models.Tag)
            .values(name=name, reference_count=0)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        self.session.flush()
        engine_delta.record_tags_dirty(self.session, [name])
        tag = self.session.execute(select(models.Tag).where(models.Tag.name == name)).scalar_one()
        return tag.id

    def _get_or_create_tag_id(self, name: str) -> int:
        return self.get_or_create_tag_id(name)

    def load_tag_names(self, tag_ids: set[int]) -> dict[int, str]:
        """Return ``{tag.id: tag.name}`` for *tag_ids*."""
        if not tag_ids:
            return {}
        rows = self.session.execute(
            select(models.Tag.id, models.Tag.name).where(models.Tag.id.in_(tag_ids))
        ).all()
        return {t[0]: t[1] for t in rows}

    def _load_tag_names(self, tag_ids: set[int]) -> dict[int, str]:
        return self.load_tag_names(tag_ids)

    @staticmethod
    def _unaliased_zero_predicate():
        return (
            models.Tag.reference_count <= 0,
            ~exists().where(
                or_(
                    models.TagAlias.source == models.Tag.id,
                    models.TagAlias.target == models.Tag.id,
                )
            ),
        )

    def prune_unused_tags(self, tag_names: Iterable[str] | None = None) -> int:
        """Delete zero-reference tag rows not referenced by any ``tag_alias``."""
        stmt = _delete(models.Tag).where(*self._unaliased_zero_predicate())
        if tag_names is not None:
            names = sorted({n for n in tag_names if n})
            if not names:
                return 0
            stmt = stmt.where(models.Tag.name.in_(names))
        deleted = list(self.session.execute(stmt.returning(models.Tag.name)).scalars().all())
        if deleted:
            engine_delta.record_tags_dirty(self.session, deleted)
        return len(deleted)

    def prune_unused_tag_ids(self, tag_ids: Iterable[int]) -> int:
        """Delete zero-reference tag rows by ID when no ``tag_alias`` references them."""
        ids = sorted(set(tag_ids))
        if not ids:
            return 0
        stmt = (
            _delete(models.Tag)
            .where(*self._unaliased_zero_predicate(), models.Tag.id.in_(ids))
            .returning(models.Tag.name)
        )
        deleted = list(self.session.execute(stmt).scalars().all())
        if deleted:
            engine_delta.record_tags_dirty(self.session, deleted)
        return len(deleted)
