"""Settings repository — runtime key-value configuration stored in the DB.

Values are stored as plain strings; callers interpret them (booleans as
``"true"`` / ``"false"``, etc.).  A missing key means "unset" and the
caller decides the default.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemySettingRepository(BaseRepository):
    """Repository for runtime settings."""

    async def get_value(self, key: str) -> str | None:
        row = self.session.execute(
            select(models.Setting).where(models.Setting.key == key)
        ).scalar_one_or_none()
        return row.value if row else None

    async def set_value(self, key: str, value: str) -> str:
        """Upsert a setting value and return it."""
        self.session.execute(
            pg_insert(models.Setting)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=[models.Setting.key],
                set_={"value": value},
            )
        )
        return value

    async def get_bool(self, key: str, default: bool = False) -> bool:
        """Read a boolean setting; missing/unparsable → *default*."""
        value = await self.get_value(key)
        if value is None:
            return default
        return value.strip().lower() in ("1", "true", "yes", "on")
