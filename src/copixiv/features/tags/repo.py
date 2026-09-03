"""Tag repository — tags, preferences, and aliases.

TagAlias uses integer FKs (source, target) → tag.id.  The API layer
continues to work with tag *names*; this repository handles the
name ↔ id translation internally.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, update as _update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository


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
            pg_insert(models.Tag).values(name=name, reference_count=0)
            .on_conflict_do_nothing(index_elements=["name"])
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
        return self._update_attrs(models.TagPreference, pref_id, pref_data)

    async def delete_preference(self, pref_id: int) -> bool:
        return self._delete_by_id(models.TagPreference, pref_id)

    async def reorder_preferences(self, ids: list[int]) -> bool:
        return self._reorder(models.TagPreference, "sort_index", ids)

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

        id_to_name = self._load_tag_names(
            {row[1] for row in rows} | {row[2] for row in rows}
        )
        return [
            {
                "id": row[0],
                "source": id_to_name.get(row[1], f"<unknown:{row[1]}>"),
                "target": id_to_name.get(row[2], f"<unknown:{row[2]}>"),
            }
            for row in rows
        ]

    def _load_tag_names(self, tag_ids: set[int]) -> dict[int, str]:
        """Return ``{tag.id: tag.name}`` for the given ids (missing → absent)."""
        if not tag_ids:
            return {}
        rows = self.session.execute(
            select(models.Tag.id, models.Tag.name).where(
                models.Tag.id.in_(tag_ids)
            )
        ).all()
        return {t[0]: t[1] for t in rows}

    def get_alias_map_sync(self) -> dict[str, str]:
        """Return {source_tag_name: target_tag_name} for all aliases (sync)."""
        rows = self.session.execute(
            select(
                models.TagAlias.source,
                models.TagAlias.target,
            )
        ).all()

        id_to_name = self._load_tag_names(
            {src for src, tgt in rows} | {tgt for src, tgt in rows}
        )
        return {
            id_to_name.get(src, str(src)): id_to_name.get(tgt, str(tgt))
            for src, tgt in rows
        }

    async def get_alias_map(self) -> dict[str, str]:
        return self.get_alias_map_sync()

    async def create_alias(self, alias_data: dict) -> dict:
        """Create a tag alias.  *alias_data* should have 'source' and 'target'
        as tag *names* — they are resolved to tag IDs internally.

        Raises:
            ValidationError: If the source tag is already aliased
                (unique constraint) — mapped to 400 instead of a raw
                IntegrityError 500.
        """
        from sqlalchemy.exc import IntegrityError

        from copixiv.core.exceptions import ValidationError

        source_name = alias_data["source"]
        target_name = alias_data["target"]

        source_id = self._get_or_create_tag_id(source_name)
        target_id = self._get_or_create_tag_id(target_name)

        alias = models.TagAlias(source=source_id, target=target_id)
        self.session.add(alias)
        try:
            # SAVEPOINT — a duplicate alias fails only this statement,
            # leaving the surrounding transaction intact.
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as exc:
            raise ValidationError(
                f"Tag '{source_name}' is already aliased"
            ) from exc

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

        Uses a single array operation: ``UPDATE novel SET tags =
        array_replace(tags, :source, :target) WHERE :source = ANY(tags)``.
        The statement-level ``sync_tag_refs`` trigger fires on the tags
        UPDATE and adjusts ``reference_count`` for both tags automatically
        (one aggregated delta set per statement).  Returns the number
        of novels affected.
        """
        src_tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == source)
        ).scalar_one_or_none()
        if not src_tag:
            return 0
        self._get_or_create_tag_id(target)

        result = self.session.execute(
            _update(models.Novel)
            .where(models.Novel.tags.contains([source]))
            .values(tags=func.array_replace(models.Novel.tags, source, target))
        )
        return result.rowcount or 0
