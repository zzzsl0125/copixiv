"""Settings repository — runtime key-value configuration stored in the DB.

Values are stored as plain strings; callers interpret them (booleans as
``"true"`` / ``"false"``, etc.).  A missing key means "unset" and the
caller decides the default.
"""

import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from copixiv.core.search import EXCLUDE_BLOCKED_SETTING_KEY
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemySettingRepository(BaseRepository):
    """Repository for runtime settings."""

    def get_value(self, key: str) -> str | None:
        row = self.session.execute(
            select(models.Setting).where(models.Setting.key == key)
        ).scalar_one_or_none()
        return row.value if row else None

    def set_value(self, key: str, value: str) -> str:
        """Upsert a setting value and return it."""
        self.session.execute(
            pg_insert(models.Setting)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=[models.Setting.key],
                set_={"value": value},
            )
        )
        if key == EXCLUDE_BLOCKED_SETTING_KEY:
            engine_delta.record_exclude_setting_dirty(self.session)
        return value

    _TRUTHY = frozenset({"1", "true", "yes", "on"})
    _FALSY = frozenset({"0", "false", "no", "off"})

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Read a boolean setting; missing/unparsable → *default*."""
        value = self.get_value(key)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in self._TRUTHY:
            return True
        if normalized in self._FALSY:
            return False
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        """Read an integer setting (JSON-encoded or raw int string)."""
        raw = self.get_value(key)
        if not raw:
            return default
        try:
            return int(json.loads(raw))
        except (TypeError, ValueError):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

    def set_int(self, key: str, value: int) -> None:
        """Persist an integer setting using JSON serialization."""
        self.set_value(key, json.dumps(int(value)))
