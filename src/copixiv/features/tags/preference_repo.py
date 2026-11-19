"""Tag preference (favourite / blocked) repository."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository


class TagPreferenceRepository(BaseRepository):
    """Repository for tag preference CRUD and ordering."""

    def get_preferences(self) -> Sequence[Any]:
        stmt = select(models.TagPreference).order_by(models.TagPreference.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    def create_preference(self, pref_data: dict) -> Any:
        pref = models.TagPreference(**pref_data)
        self.session.add(pref)
        self.session.flush()
        engine_delta.record_blocked_tags_dirty(self.session)
        return pref

    def update_preference(self, pref_id: int, pref_data: dict) -> Any | None:
        pref = self._update_attrs(models.TagPreference, pref_id, pref_data)
        if pref is not None:
            engine_delta.record_blocked_tags_dirty(self.session)
        return pref

    def delete_preference(self, pref_id: int) -> bool:
        deleted = self._delete_by_id(models.TagPreference, pref_id)
        if deleted:
            engine_delta.record_blocked_tags_dirty(self.session)
        return deleted

    def reorder_preferences(self, ids: list[int]) -> int:
        return self._reorder(models.TagPreference, "sort_index", ids)
