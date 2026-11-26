"""Repository for ``source_config`` and ``source_credential`` persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import delete_by_id


class SQLAlchemySourceRepository:
    """Repository for per-source runtime configs and credentials."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Source configurations
    # ------------------------------------------------------------------

    def list_configs(self) -> Sequence[models.SourceConfig]:
        """Return all persisted ``source_config`` rows ordered by source key."""
        stmt = select(models.SourceConfig).order_by(models.SourceConfig.source)
        return list(self.session.execute(stmt).scalars().all())

    def get_config(self, source: str) -> models.SourceConfig | None:
        """Load one ``source_config`` row by primary key."""
        return self.session.get(models.SourceConfig, source)

    def upsert_config(
        self,
        source: str,
        *,
        enabled: bool,
        config: dict[str, Any],
    ) -> models.SourceConfig:
        """Insert or update a ``source_config`` row and return it."""
        now = datetime.now(UTC)
        payload = dict(config)
        stmt = (
            postgresql_insert(models.SourceConfig)
            .values(
                source=source,
                enabled=enabled,
                config=payload,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["source"],
                set_={
                    "enabled": enabled,
                    "config": payload,
                    "updated_at": now,
                },
            )
            .returning(models.SourceConfig)
        )
        row = self.session.scalars(
            stmt,
            execution_options={"populate_existing": True},
        ).one()
        self.session.flush()
        return row

    def seed_config_if_missing(
        self,
        source: str,
        *,
        enabled: bool,
        config: dict[str, Any],
    ) -> bool:
        """Insert default ``source_config`` only when no row exists for *source*."""
        now = datetime.now(UTC)
        stmt = (
            postgresql_insert(models.SourceConfig)
            .values(
                source=source,
                enabled=enabled,
                config=dict(config),
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["source"])
            .returning(models.SourceConfig.source)
        )
        inserted = self.session.execute(stmt).scalar_one_or_none()
        return inserted is not None

    # ------------------------------------------------------------------
    # Source credentials
    # ------------------------------------------------------------------

    def list_credentials(self, source: str) -> Sequence[models.SourceCredential]:
        """Return credentials for *source* ordered by ``(sort_index, id)``."""
        stmt = (
            select(models.SourceCredential)
            .where(models.SourceCredential.source == source)
            .order_by(models.SourceCredential.sort_index, models.SourceCredential.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_credential(self, cred_id: int) -> models.SourceCredential | None:
        """Load one ``source_credential`` row by primary key."""
        return self.session.get(models.SourceCredential, cred_id)

    def create_credential(
        self,
        source: str,
        *,
        name: str,
        kind: str,
        secret: dict[str, Any],
        valid: bool = True,
        sort_index: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> models.SourceCredential:
        """Insert a new ``source_credential`` row and flush."""
        if sort_index is None:
            sort_index = int(
                self.session.execute(
                    select(func.coalesce(func.max(models.SourceCredential.sort_index), -1) + 1).where(
                        models.SourceCredential.source == source
                    )
                ).scalar_one()
            )
        now = datetime.now(UTC)
        cred = models.SourceCredential(
            source=source,
            name=name,
            kind=kind,
            secret=dict(secret),
            valid=valid,
            sort_index=sort_index,
            meta=dict(meta or {}),
            created_at=now,
            updated_at=now,
        )
        self.session.add(cred)
        self.session.flush()
        return cred

    def update_credential(
        self,
        cred_id: int,
        *,
        name: str | None = None,
        secret: dict[str, Any] | None = None,
        valid: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> models.SourceCredential | None:
        """Update mutable columns on an existing ``source_credential`` row."""
        cred = self.session.get(models.SourceCredential, cred_id)
        if cred is None:
            return None
        if name is not None:
            cred.name = name
        if secret is not None:
            cred.secret = dict(secret)
        if valid is not None:
            cred.valid = valid
        if meta is not None:
            cred.meta = dict(meta)
        cred.updated_at = datetime.now(UTC)
        self.session.flush()
        return cred

    def delete_credential(self, cred_id: int) -> bool:
        """Delete one ``source_credential`` row by primary key."""
        return delete_by_id(self.session, models.SourceCredential, cred_id)

    def reorder_credentials(self, source: str, ids: list[int]) -> int:
        """Assign dense ``sort_index`` values ``0..n-1`` scoped strictly to *source*."""
        rows = list(self.list_credentials(source))
        by_id = {int(obj.id): obj for obj in rows}
        ordered: list[models.SourceCredential] = []
        seen: set[int] = set()
        matched = 0
        for obj_id in ids:
            if obj_id in seen:
                continue
            seen.add(obj_id)
            obj = by_id.get(obj_id)
            if obj is not None:
                ordered.append(obj)
                matched += 1
        for obj in rows:
            if int(obj.id) not in seen:
                ordered.append(obj)
        for idx, obj in enumerate(ordered):
            obj.sort_index = idx
        self.session.flush()
        return matched

    def set_unique_meta_flag(
        self,
        source: str,
        cred_id: int,
        flag: str,
        value: bool,
    ) -> bool:
        """Set a boolean *flag* in ``meta``, clearing it on all sibling rows when ``True``."""
        cred = self.session.get(models.SourceCredential, cred_id)
        if cred is None or cred.source != source:
            return False
        if value:
            self.session.execute(
                sa_text(
                    """
                    UPDATE source_credential
                    SET meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object(CAST(:flag AS text), false),
                        updated_at = now()
                    WHERE source = :source
                      AND id != :cred_id
                      AND COALESCE((meta ->> CAST(:flag AS text))::boolean, false) IS TRUE
                    """
                ),
                {"source": source, "cred_id": cred_id, "flag": flag},
            )
        cred.meta = {**(cred.meta or {}), flag: value}
        cred.updated_at = datetime.now(UTC)
        self.session.flush()
        return True


__all__ = ["SQLAlchemySourceRepository"]
