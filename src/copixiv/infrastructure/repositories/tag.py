"""Tag repository — tags, preferences, and aliases.

TagAlias uses integer FKs (source, target) → tag.id.  The API layer
continues to work with tag *names*; this repository handles the
name ↔ id translation internally.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, delete as _delete, update as _update, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from .base import BaseRepository


class SQLAlchemyTagRepository(BaseRepository):
    """Repository for tag CRUD, preferences, and aliases."""

    def __init__(self, session: Session):
        super().__init__(session)

    # -- tags ---------------------------------------------------------------

    async def get_all(self) -> Sequence[Any]:
        return list(self.session.execute(
            select(models.Tag).order_by(models.Tag.name)
        ).scalars().all())

    def _get_or_create_tag_id(self, name: str) -> int:
        """Return the tag.id for *name*, creating the tag if it doesn't exist."""
        tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == name)
        ).scalar_one_or_none()
        if tag:
            return tag.id
        self.session.execute(
            sqlite_insert(models.Tag).values(name=name, reference_count=0)
        )
        self.session.flush()
        tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == name)
        ).scalar_one()
        return tag.id

    # -- preferences --------------------------------------------------------

    async def get_preferences(self) -> Sequence[Any]:
        stmt = select(models.TagPreference).order_by(models.TagPreference.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    async def create_preference(self, pref_data: dict) -> Any:
        pref = models.TagPreference(**pref_data)
        self.session.add(pref)
        self.session.flush()
        return pref

    async def update_preference(self, pref_id: int, pref_data: dict) -> Any | None:
        pref = self.session.get(models.TagPreference, pref_id)
        if pref is None:
            return None
        for k, v in pref_data.items():
            if hasattr(pref, k):
                setattr(pref, k, v)
        return pref

    async def delete_preference(self, pref_id: int) -> bool:
        pref = self.session.get(models.TagPreference, pref_id)
        if pref is None:
            return False
        self.session.delete(pref)
        return True

    async def reorder_preferences(self, ids: list[int]) -> bool:
        for idx, pref_id in enumerate(ids):
            pref = self.session.get(models.TagPreference, pref_id)
            if pref is not None:
                pref.sort_index = idx
        return True

    # -- aliases ------------------------------------------------------------
    # source/target are stored as integer FKs → tag.id but exposed as tag
    # names for API compatibility.

    async def get_aliases(self) -> Sequence[dict]:
        """Return all tag aliases with source/target as tag names."""
        rows = self.session.execute(
            select(
                models.TagAlias.id,
                models.TagAlias.source,
                models.TagAlias.target,
            ).order_by(models.TagAlias.id)
        ).all()

        # Resolve IDs → names
        tag_ids: set[int] = set()
        for row in rows:
            tag_ids.add(row[1])
            tag_ids.add(row[2])

        id_to_name: dict[int, str] = {}
        if tag_ids:
            tag_rows = self.session.execute(
                select(models.Tag.id, models.Tag.name).where(
                    models.Tag.id.in_(tag_ids)
                )
            ).all()
            id_to_name = {t[0]: t[1] for t in tag_rows}

        return [
            {
                "id": row[0],
                "source": id_to_name.get(row[1], f"<unknown:{row[1]}>"),
                "target": id_to_name.get(row[2], f"<unknown:{row[2]}>"),
            }
            for row in rows
        ]

    def get_alias_map_sync(self) -> dict[str, str]:
        """Return {source_tag_name: target_tag_name} for all aliases (sync)."""
        rows = self.session.execute(
            select(
                models.TagAlias.source,
                models.TagAlias.target,
            )
        ).all()

        tag_ids: set[int] = set()
        for src, tgt in rows:
            tag_ids.add(src)
            tag_ids.add(tgt)

        id_to_name: dict[int, str] = {}
        if tag_ids:
            tag_rows = self.session.execute(
                select(models.Tag.id, models.Tag.name).where(
                    models.Tag.id.in_(tag_ids)
                )
            ).all()
            id_to_name = {t[0]: t[1] for t in tag_rows}

        return {
            id_to_name.get(src, str(src)): id_to_name.get(tgt, str(tgt))
            for src, tgt in rows
        }

    async def get_alias_map(self) -> dict[str, str]:
        return self.get_alias_map_sync()

    async def create_alias(self, alias_data: dict) -> dict:
        """Create a tag alias.  *alias_data* should have 'source' and 'target'
        as tag *names* — they are resolved to tag IDs internally."""
        source_name = alias_data["source"]
        target_name = alias_data["target"]

        source_id = self._get_or_create_tag_id(source_name)
        target_id = self._get_or_create_tag_id(target_name)

        alias = models.TagAlias(source=source_id, target=target_id)
        self.session.add(alias)
        self.session.flush()

        return {
            "id": alias.id,
            "source": source_name,
            "target": target_name,
        }

    async def delete_alias(self, alias_id: int) -> bool:
        alias = self.session.get(models.TagAlias, alias_id)
        if alias is None:
            return False
        self.session.delete(alias)
        return True

    async def suggest_aliases(
        self, limit: int = 5, offset: int = 0, target_tag: str | None = None
    ) -> dict:
        """Suggest alias mappings by finding tags with similar names.

        Tags are ranked by ``reference_count`` descending.  For each tag,
        candidates with similar names are found via SQL ILIKE queries
        (same-first-character prefix or substring containment).  Tags
        already participating in any alias mapping are excluded.

        Mirrors the v1 ``TagAliasRepository.get_suggestions`` algorithm.
        """
        # -- tag IDs already used in aliases -----------------------------------
        alias_rows = self.session.execute(
            select(models.TagAlias.source, models.TagAlias.target)
        ).all()
        excluded_ids: set[int] = set()
        for src_id, tgt_id in alias_rows:
            excluded_ids.add(src_id)
            excluded_ids.add(tgt_id)

        # -- target-tag mode --------------------------------------------------
        if target_tag:
            tag = self.session.execute(
                select(models.Tag).where(models.Tag.name == target_tag)
            ).scalar_one_or_none()
            if not tag or tag.id in excluded_ids:
                return {"items": [], "next_offset": 0}
            candidates = self._find_similar_tags(tag, excluded_ids)
            item = self._make_suggest_item(tag, candidates)
            return {"items": [item] if candidates else [], "next_offset": 0}

        # -- general mode: paginate through tags by reference_count ------------
        results: list[dict] = []
        current_offset = offset

        while len(results) < limit:
            tags = self.session.execute(
                select(models.Tag)
                .order_by(models.Tag.reference_count.desc())
                .offset(current_offset).limit(50)
            ).scalars().all()
            if not tags:
                break

            for tag in tags:
                current_offset += 1
                if tag.id in excluded_ids or tag.reference_count == 0:
                    continue

                candidates = self._find_similar_tags(tag, excluded_ids)
                if candidates:
                    results.append(self._make_suggest_item(tag, candidates))
                    if len(results) >= limit:
                        break

        return {"items": results, "next_offset": current_offset}

    # ------------------------------------------------------------------
    # suggest helpers
    # ------------------------------------------------------------------

    def _find_similar_tags(self, tag, excluded_ids: set[int]) -> list[dict]:
        """Return up to 50 tags whose names are similar to *tag*.

        Candidates match when they share the same first character (prefix)
        or contain *tag.name* as a substring.  Tags whose IDs appear in
        *excluded_ids* are filtered out.
        """
        first_char = tag.name[0] if tag.name else ""
        if not first_char:
            return []

        candidates = self.session.execute(
            select(models.Tag)
            .where(
                models.Tag.name.ilike(f"{first_char}%")
                | models.Tag.name.ilike(f"%{tag.name}%")
            )
            .where(models.Tag.id != tag.id)
            .order_by(models.Tag.reference_count.desc())
            .limit(50)
        ).scalars().all()

        return [
            {"id": c.id, "name": c.name, "reference_count": c.reference_count}
            for c in candidates
            if c.id not in excluded_ids
        ]

    @staticmethod
    def _make_suggest_item(tag, candidates) -> dict:
        return {
            "target": {
                "id": tag.id,
                "name": tag.name,
                "reference_count": tag.reference_count,
            },
            "candidates": candidates,
        }

    async def apply_alias_retroactively(self, source: str, target: str) -> int:
        """Replace all occurrences of *source* tag with *target* tag on novels.

        Args:
            source: Source tag *name* (will be resolved to tag ID).
            target: Target tag *name* (will be resolved to tag ID).

        Returns:
            Number of novels affected.
        """
        # Find the tag IDs
        src_tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == source)
        ).scalar_one_or_none()
        if not src_tag:
            return 0

        tgt_tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == target)
        ).scalar_one_or_none()
        if not tgt_tag:
            self.session.execute(
                sqlite_insert(models.Tag).values(name=target)
            )
            self.session.flush()
            tgt_tag = self.session.execute(
                select(models.Tag).where(models.Tag.name == target)
            ).scalar_one()

        # Find novels with source tag
        stmt = select(models.NovelTag.novel_id).where(
            models.NovelTag.tag_id == src_tag.id
        )
        novel_ids = self.session.execute(stmt).scalars().all()
        if not novel_ids:
            return 0

        # Batch insert target links (skip existing)
        self.session.execute(
            sqlite_insert(models.NovelTag).values([
                {"novel_id": nid, "tag_id": tgt_tag.id} for nid in novel_ids
            ]).on_conflict_do_nothing(index_elements=["novel_id", "tag_id"])
        )

        # Batch delete source links
        self.session.execute(
            _delete(models.NovelTag).where(
                models.NovelTag.novel_id.in_(novel_ids),
                models.NovelTag.tag_id == src_tag.id,
            )
        )

        # Update counts
        affected = len(novel_ids)
        self.session.execute(
            _update(models.Tag)
            .where(models.Tag.id == src_tag.id)
            .values(reference_count=models.Tag.reference_count - affected)
        )

        already_had = self.session.execute(
            select(func.count()).select_from(models.NovelTag).where(
                models.NovelTag.novel_id.in_(novel_ids),
                models.NovelTag.tag_id == tgt_tag.id,
            )
        ).scalar() or 0
        newly_added = affected - already_had
        if newly_added > 0:
            self.session.execute(
                _update(models.Tag)
                .where(models.Tag.id == tgt_tag.id)
                .values(reference_count=models.Tag.reference_count + newly_added)
            )

        return affected
