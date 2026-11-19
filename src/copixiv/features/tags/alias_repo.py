"""Tag alias repository."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from copixiv.core.exceptions import ValidationError
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository

from .tag_repo import TagRepository


class TagAliasRepository(BaseRepository):
    """Repository for single-hop tag alias CRUD and lookups."""

    def get_aliases(self) -> Sequence[dict]:
        """Return all tag aliases with source/target as tag names."""
        rows = self.session.execute(
            select(
                models.TagAlias.id,
                models.TagAlias.source,
                models.TagAlias.target,
            ).order_by(models.TagAlias.id)
        ).all()

        tag_repo = TagRepository(self.session)
        id_to_name = tag_repo.load_tag_names({row[1] for row in rows} | {row[2] for row in rows})
        return [
            {
                "id": row[0],
                "source": id_to_name.get(row[1], f"<unknown:{row[1]}>"),
                "target": id_to_name.get(row[2], f"<unknown:{row[2]}>"),
            }
            for row in rows
        ]

    def get_alias_map(self) -> dict[str, str]:
        """Return ``{source_tag_name: target_tag_name}`` for all aliases."""
        rows = self.session.execute(select(models.TagAlias.source, models.TagAlias.target)).all()

        tag_repo = TagRepository(self.session)
        id_to_name = tag_repo.load_tag_names({src for src, _tgt in rows} | {tgt for _src, tgt in rows})
        return {id_to_name.get(src, str(src)): id_to_name.get(tgt, str(tgt)) for src, tgt in rows}

    def create_alias(self, alias_data: dict) -> dict:
        """Create a tag alias enforcing single-hop and acyclic invariants."""
        source_name = alias_data["source"]
        target_name = alias_data["target"]
        if source_name == target_name:
            raise ValidationError("Source and target tags cannot be the same")

        tag_repo = TagRepository(self.session)
        source_id = tag_repo.get_or_create_tag_id(source_name)
        target_id = tag_repo.get_or_create_tag_id(target_name)

        existing_reverse = self.session.execute(
            select(models.TagAlias.target).where(models.TagAlias.source == target_id)
        ).scalar_one_or_none()
        if existing_reverse is not None:
            if existing_reverse == source_id:
                raise ValidationError(
                    f"Cannot create alias cycle between '{source_name}' and '{target_name}'"
                )
            raise ValidationError(
                f"Target tag '{target_name}' is already aliased to another tag "
                "(multi-hop aliases are not supported)"
            )

        incoming_source = self.session.execute(
            select(models.TagAlias.id).where(models.TagAlias.target == source_id).limit(1)
        ).scalar_one_or_none()
        if incoming_source is not None:
            raise ValidationError(
                f"Source tag '{source_name}' is already the target of another alias "
                "(multi-hop aliases are not supported)"
            )

        alias = models.TagAlias(source=source_id, target=target_id)
        self.session.add(alias)
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as exc:
            raise ValidationError(f"Tag '{source_name}' is already aliased") from exc

        return {
            "id": alias.id,
            "source": source_name,
            "target": target_name,
        }

    def delete_alias(self, alias_id: int) -> bool:
        alias = self.session.get(models.TagAlias, alias_id)
        if alias is None:
            return False
        tag_ids = {alias.source, alias.target}
        self.session.delete(alias)
        self.session.flush()
        engine_delta.record_aliases_dirty(self.session)
        TagRepository(self.session).prune_unused_tag_ids(tag_ids)
        return True
