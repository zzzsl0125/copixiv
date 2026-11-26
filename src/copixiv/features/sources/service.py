"""Application service coordinating source configuration, credentials, and hot reloads."""

from __future__ import annotations

import copy
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.orm import Session, sessionmaker

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import SOURCE_PIXIV
from copixiv.core.sources import (
    CredentialKindSpec,
    SourceCapability,
    SourceCredentialRecord,
    SourcePlugin,
    SourceRuntimeConfig,
    SourceTestResult,
)
from copixiv.db import models
from copixiv.db.tx import read_transaction, run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.sources.repo import SQLAlchemySourceRepository
from copixiv.sources.http import SourceHttpManager
from copixiv.sources.registry import SourceRegistry

if TYPE_CHECKING:
    from copixiv.config import AppConfig

_RELOAD_LOCK = threading.Lock()
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _mask_secret_value(value: Any) -> str:
    """Mask a secret string so at most its last 4 characters are visible."""
    text = str(value) if value is not None else ""
    if len(text) <= 4:
        return "****"
    return f"****{text[-4:]}"


def _coerce_cred_bool(label: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
    raise ValidationError(f"{label}必须是布尔值")


def _resolve_kind_spec(plugin: SourcePlugin, kind: str | None) -> CredentialKindSpec:
    kinds = plugin.manifest.credential_kinds
    if kind is None:
        if len(kinds) == 1:
            return kinds[0]
        raise ValidationError("请指定凭据类型")
    for spec in kinds:
        if spec.kind == kind:
            return spec
    raise ValidationError(f"不支持的凭据类型: {kind}")


def _find_kind_spec(plugin: SourcePlugin, kind: str) -> CredentialKindSpec | None:
    for spec in plugin.manifest.credential_kinds:
        if spec.kind == kind:
            return spec
    return None


def _normalize_meta(
    kind_spec: CredentialKindSpec,
    raw_meta: dict[str, Any] | None,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = dict(existing or {})
    for field in kind_spec.meta_fields:
        if raw_meta is not None and field.key in raw_meta:
            val = raw_meta[field.key]
        elif field.key in base:
            val = base[field.key]
        else:
            val = copy.deepcopy(field.default)

        if val is None:
            if field.default is not None:
                base[field.key] = copy.deepcopy(field.default)
            else:
                base.pop(field.key, None)
            continue

        if field.field_type == "boolean":
            base[field.key] = _coerce_cred_bool(field.label, val)
        else:
            if not isinstance(val, str):
                raise ValidationError(f"{field.label}必须是字符串")
            base[field.key] = val.strip()

    known_keys = {f.key for f in kind_spec.meta_fields}
    for flag in kind_spec.unique_meta_flags:
        if flag not in known_keys and raw_meta is not None and flag in raw_meta:
            base[flag] = _coerce_cred_bool(flag, raw_meta[flag])
    return base


class SourceService:
    """Manages source configurations, credentials, runtime caching, and plugin reloads."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: SourceRegistry,
        http_manager: SourceHttpManager,
        app_config: AppConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.registry = registry
        self.http_manager = http_manager
        self._app_config = app_config
        self._runtime_cache: dict[str, SourceRuntimeConfig] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_plugin(self, source: str) -> SourcePlugin:
        plugin = self.registry.get(source)
        if plugin is None:
            raise NotFoundError(f"Source {source} not found")
        return plugin

    def _default_values_for(self, plugin: SourcePlugin) -> dict[str, Any]:
        defaults = plugin.manifest.default_config()
        if plugin.manifest.source == SOURCE_PIXIV and self._app_config is not None:
            defaults["min_interval"] = self._app_config.pixiv_client.min_interval
            defaults["cooling_duration"] = self._app_config.pixiv_client.cooling_duration
            defaults["max_concurrency"] = self._app_config.pixiv_client.max_concurrency
        return defaults

    def _build_runtime(
        self,
        plugin: SourcePlugin,
        row: models.SourceConfig | None,
    ) -> SourceRuntimeConfig:
        defaults = self._default_values_for(plugin)
        if row is None:
            return SourceRuntimeConfig.from_values(
                plugin.manifest.source,
                plugin.manifest.enabled_by_default,
                defaults,
            )
        merged = {**defaults, **(row.config or {})}
        return SourceRuntimeConfig.from_values(plugin.manifest.source, bool(row.enabled), merged)

    @staticmethod
    def _to_credential_record(cred: models.SourceCredential) -> SourceCredentialRecord:
        return SourceCredentialRecord(
            id=int(cred.id),
            source=cred.source,
            name=cred.name,
            kind=cred.kind,
            secret=dict(cred.secret or {}),
            valid=bool(cred.valid),
            sort_index=int(cred.sort_index),
            meta=dict(cred.meta or {}),
        )

    def _seed_defaults(self, session: Session) -> None:
        repo = SQLAlchemySourceRepository(session)
        for plugin in self.registry.all_plugins():
            defaults = self._default_values_for(plugin)
            repo.seed_config_if_missing(
                plugin.manifest.source,
                enabled=plugin.manifest.enabled_by_default,
                config=defaults,
            )

    def _build_source_item(
        self,
        plugin: SourcePlugin,
        config_row: models.SourceConfig | None,
        creds: Sequence[models.SourceCredential],
    ) -> dict[str, Any]:
        runtime = self._build_runtime(plugin, config_row)
        manifest_data = plugin.manifest.model_dump(mode="json")
        manifest_data["capabilities"] = sorted(c.value for c in plugin.manifest.capabilities)
        return {
            "source": plugin.manifest.source,
            "enabled": runtime.enabled,
            "config": dict(runtime.values),
            "manifest": manifest_data,
            "credential_count": len(creds),
            "valid_credential_count": sum(1 for c in creds if c.valid),
        }

    @staticmethod
    def _credential_to_masked_dict(
        cred: models.SourceCredential,
        kind_spec: CredentialKindSpec | None,
    ) -> dict[str, Any]:
        secret_raw = dict(cred.secret or {})
        masked_secret: dict[str, Any] = {}
        if kind_spec is None:
            for k, v in secret_raw.items():
                masked_secret[k] = _mask_secret_value(v)
        else:
            field_types = {f.key: f.field_type for f in kind_spec.secret_fields}
            for k, v in secret_raw.items():
                if field_types.get(k, "secret") == "secret":
                    masked_secret[k] = _mask_secret_value(v)
                else:
                    masked_secret[k] = v

        meta_defaults = (
            {f.key: copy.deepcopy(f.default) for f in kind_spec.meta_fields if f.default is not None}
            if kind_spec is not None
            else {}
        )
        meta = {**meta_defaults, **(cred.meta or {})}
        return {
            "id": int(cred.id),
            "source": cred.source,
            "name": cred.name,
            "kind": cred.kind,
            "secret": masked_secret,
            "valid": bool(cred.valid),
            "sort_index": int(cred.sort_index),
            "meta": meta,
        }

    # ------------------------------------------------------------------
    # Lifecycle & runtime queries
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Seed default source configs, load DB state into cache, and trigger ``on_reload``."""
        uow = SqlUnitOfWork(self._session_factory)
        with _RELOAD_LOCK:
            with uow.begin():
                self._seed_defaults(uow.session)

            with uow.begin(read_only=True):
                repo = SQLAlchemySourceRepository(uow.session)
                configs_by_source = {row.source: row for row in repo.list_configs()}
                snapshots: list[tuple[SourcePlugin, SourceRuntimeConfig, list[SourceCredentialRecord]]] = []
                for plugin in self.registry.all_plugins():
                    src = plugin.manifest.source
                    runtime = self._build_runtime(plugin, configs_by_source.get(src))
                    creds = [self._to_credential_record(c) for c in repo.list_credentials(src)]
                    snapshots.append((plugin, runtime, creds))

            with self._lock:
                for plugin, runtime, _ in snapshots:
                    self._runtime_cache[plugin.manifest.source] = runtime

            for plugin, runtime, creds in snapshots:
                plugin.on_reload(runtime, creds)

    def is_enabled(self, source: str) -> bool:
        """Return whether *source* is registered and enabled."""
        with self._lock:
            cached = self._runtime_cache.get(source)
        if cached is not None:
            return cached.enabled
        return self.registry.get(source) is not None

    def require_enabled(self, source: str) -> SourcePlugin:
        """Return the registered plugin for *source* or raise ``ValidationError`` if unknown/disabled."""
        plugin = self.registry.get(source)
        if plugin is None:
            raise ValidationError(f"未知数据源: {source}")
        if not self.is_enabled(source):
            raise ValidationError(f"数据源 {plugin.manifest.display_name} 已禁用")
        return plugin

    def get_runtime_config(
        self,
        source: str,
        session: Session | None = None,
    ) -> SourceRuntimeConfig:
        """Return merged runtime config from *session* or the in-memory cache."""
        plugin = self.registry.get(source)
        if session is not None:
            row = SQLAlchemySourceRepository(session).get_config(source)
            if plugin is not None:
                return self._build_runtime(plugin, row)
            if row is not None:
                return SourceRuntimeConfig.from_values(source, bool(row.enabled), dict(row.config or {}))
            return SourceRuntimeConfig(source=source, enabled=False)

        with self._lock:
            cached = self._runtime_cache.get(source)
        if cached is not None:
            return cached
        if plugin is not None:
            return self._build_runtime(plugin, None)
        return SourceRuntimeConfig(source=source, enabled=False)

    def get_credentials(
        self,
        source: str,
        session: Session | None = None,
    ) -> list[SourceCredentialRecord]:
        """Load persisted credentials for *source* ordered by ``(sort_index, id)``."""
        if session is not None:
            return [
                self._to_credential_record(c)
                for c in SQLAlchemySourceRepository(session).list_credentials(source)
            ]
        uow = SqlUnitOfWork(self._session_factory)
        with uow.begin(read_only=True):
            return [
                self._to_credential_record(c)
                for c in SQLAlchemySourceRepository(uow.session).list_credentials(source)
            ]

    def reload_source(self, uow: SqlUnitOfWork, source: str) -> SourceRuntimeConfig:
        """Reload committed DB state for *source*, update cache, and call ``plugin.on_reload``."""
        plugin = self._require_plugin(source)
        with _RELOAD_LOCK:
            with uow.begin(read_only=True):
                repo = SQLAlchemySourceRepository(uow.session)
                runtime = self._build_runtime(plugin, repo.get_config(source))
                creds = [self._to_credential_record(c) for c in repo.list_credentials(source)]

            with self._lock:
                self._runtime_cache[source] = runtime

            plugin.on_reload(runtime, creds)
            return runtime

    def build_external_url(
        self,
        source: str,
        kind: Literal["novel", "series", "author"],
        source_key: str,
    ) -> str | None:
        """Build an external entity URL using *source*'s plugin and active runtime config."""
        return self.registry.build_external_url(
            source,
            kind,
            source_key,
            self.get_runtime_config(source),
        )

    # ------------------------------------------------------------------
    # Config & test API operations
    # ------------------------------------------------------------------

    def list_sources(self, session: Session) -> list[dict[str, Any]]:
        """List all registered sources with merged configs, manifests, and credential counts."""
        repo = SQLAlchemySourceRepository(session)
        configs_by_source = {row.source: row for row in repo.list_configs()}
        items: list[dict[str, Any]] = []
        for plugin in self.registry.all_plugins():
            src = plugin.manifest.source
            creds = repo.list_credentials(src)
            items.append(self._build_source_item(plugin, configs_by_source.get(src), creds))
        return items

    def update_config(
        self,
        uow: SqlUnitOfWork,
        source: str,
        *,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate, persist, and hot-reload source configuration."""
        plugin = self._require_plugin(source)
        defaults = self._default_values_for(plugin)

        def _write(tx: SqlUnitOfWork) -> None:
            repo = SQLAlchemySourceRepository(tx.session)
            row = repo.get_config(source)
            current_enabled = bool(row.enabled) if row is not None else plugin.manifest.enabled_by_default
            current_config = {**defaults, **(row.config or {})} if row is not None else dict(defaults)
            next_enabled = current_enabled if enabled is None else bool(enabled)
            merged_raw = {**current_config, **(config or {})}
            validated_cfg = plugin.validate_config(merged_raw)
            repo.upsert_config(source, enabled=next_enabled, config=validated_cfg)

        run_write_transaction_sync(uow, _write)
        self.reload_source(uow, source)
        with uow.begin(read_only=True):
            repo = SQLAlchemySourceRepository(uow.session)
            return self._build_source_item(
                plugin,
                repo.get_config(source),
                repo.list_credentials(source),
            )

    async def test_connection(self, uow: SqlUnitOfWork, source: str) -> SourceTestResult:
        """Test connectivity for *source* using its committed config and credentials."""
        plugin = self._require_plugin(source)
        if not plugin.manifest.has_capability(SourceCapability.TEST_CONNECTION):
            raise ValidationError(f"数据源 {plugin.manifest.display_name} 不支持连接测试")

        def _read(tx: SqlUnitOfWork) -> tuple[SourceRuntimeConfig, list[SourceCredentialRecord]]:
            repo = SQLAlchemySourceRepository(tx.session)
            runtime = self._build_runtime(plugin, repo.get_config(source))
            creds = [self._to_credential_record(c) for c in repo.list_credentials(source)]
            return runtime, creds

        runtime, creds = await read_transaction(uow, _read)
        return await plugin.test_connection(runtime, creds)

    # ------------------------------------------------------------------
    # Credential CRUD operations
    # ------------------------------------------------------------------

    def list_credentials_masked(self, uow: SqlUnitOfWork, source: str) -> list[dict[str, Any]]:
        """List credentials for *source* with secret values masked."""
        plugin = self._require_plugin(source)
        with uow.begin(read_only=True):
            creds = SQLAlchemySourceRepository(uow.session).list_credentials(source)
            return [self._credential_to_masked_dict(c, _find_kind_spec(plugin, c.kind)) for c in creds]

    def create_credential(
        self,
        uow: SqlUnitOfWork,
        source: str,
        *,
        name: str,
        kind: str | None = None,
        secret: dict[str, Any] | None = None,
        valid: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and persist a new credential, enforce unique flags, and reload *source*."""
        plugin = self._require_plugin(source)
        if (
            not plugin.manifest.has_capability(SourceCapability.AUTH_POOL)
            or not plugin.manifest.credential_kinds
        ):
            raise ValidationError(f"数据源 {plugin.manifest.display_name} 不支持凭据管理")

        kind_spec = _resolve_kind_spec(plugin, kind)
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValidationError("凭据名称不能为空")

        raw_secret = dict(secret or {})
        cleaned_secret: dict[str, Any] = {}
        for field in kind_spec.secret_fields:
            val = raw_secret.get(field.key)
            if field.field_type == "boolean":
                if val is None:
                    if field.required:
                        raise ValidationError(f"{field.label}不能为空")
                    if field.default is not None:
                        cleaned_secret[field.key] = bool(field.default)
                else:
                    cleaned_secret[field.key] = _coerce_cred_bool(field.label, val)
                continue

            if val is None:
                if field.required:
                    raise ValidationError(f"{field.label}不能为空")
                if field.default is not None:
                    cleaned_secret[field.key] = str(field.default)
                continue
            if not isinstance(val, str):
                raise ValidationError(f"{field.label}必须是字符串")
            stripped = val.strip()
            if not stripped:
                if field.required:
                    raise ValidationError(f"{field.label}不能为空")
                continue
            cleaned_secret[field.key] = stripped

        normalized_meta = _normalize_meta(kind_spec, meta)
        wanted_unique_flags = [
            flag for flag in kind_spec.unique_meta_flags if bool(normalized_meta.get(flag))
        ]
        insert_meta = dict(normalized_meta)
        for flag in wanted_unique_flags:
            insert_meta[flag] = False

        def _write(tx: SqlUnitOfWork) -> dict[str, Any]:
            repo = SQLAlchemySourceRepository(tx.session)
            created = repo.create_credential(
                source,
                name=cleaned_name,
                kind=kind_spec.kind,
                secret=cleaned_secret,
                valid=valid,
                meta=insert_meta,
            )
            for flag in wanted_unique_flags:
                repo.set_unique_meta_flag(source, int(created.id), flag, True)
            return self._credential_to_masked_dict(created, kind_spec)

        result = run_write_transaction_sync(uow, _write)
        self.reload_source(uow, source)
        return result

    def update_credential(
        self,
        uow: SqlUnitOfWork,
        source: str,
        cred_id: int,
        *,
        name: str | None = None,
        secret: dict[str, Any] | None = None,
        valid: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an existing credential (preserving masked/empty secrets) and reload *source*."""
        plugin = self._require_plugin(source)
        cleaned_name: str | None = None
        if name is not None:
            cleaned_name = name.strip()
            if not cleaned_name:
                raise ValidationError("凭据名称不能为空")

        def _write(tx: SqlUnitOfWork) -> dict[str, Any]:
            repo = SQLAlchemySourceRepository(tx.session)
            cred = repo.get_credential(cred_id)
            if cred is None or cred.source != source:
                raise NotFoundError(f"Credential {cred_id} not found")

            kind_spec = _find_kind_spec(plugin, cred.kind)
            if kind_spec is None:
                raise ValidationError(f"不支持的凭据类型: {cred.kind}")

            next_secret: dict[str, Any] | None = None
            if secret is not None:
                next_secret = dict(cred.secret or {})
                for field in kind_spec.secret_fields:
                    if field.key not in secret:
                        continue
                    val = secret[field.key]
                    if val is None or val == "":
                        continue
                    if isinstance(val, str) and val.startswith("****"):
                        continue
                    if field.field_type == "boolean":
                        next_secret[field.key] = _coerce_cred_bool(field.label, val)
                    else:
                        if not isinstance(val, str):
                            raise ValidationError(f"{field.label}必须是字符串")
                        if not val.strip():
                            raise ValidationError("凭据密钥不能为空")
                        next_secret[field.key] = val.strip()

            next_meta: dict[str, Any] | None = None
            flags_to_apply: list[tuple[str, bool]] = []
            if meta is not None:
                existing_meta = dict(cred.meta or {})
                normalized_meta = _normalize_meta(kind_spec, meta, existing=existing_meta)
                next_meta = dict(normalized_meta)
                for flag in kind_spec.unique_meta_flags:
                    if flag in meta and meta[flag] is not None:
                        desired = bool(normalized_meta.get(flag))
                        flags_to_apply.append((flag, desired))
                        if desired:
                            # Keep existing value until set_unique_meta_flag clears siblings first.
                            next_meta[flag] = bool(existing_meta.get(flag, False))

            updated = repo.update_credential(
                cred_id,
                name=cleaned_name,
                secret=next_secret,
                valid=valid,
                meta=next_meta,
            )
            assert updated is not None
            for flag, desired in flags_to_apply:
                repo.set_unique_meta_flag(source, cred_id, flag, desired)
            return self._credential_to_masked_dict(updated, kind_spec)

        result = run_write_transaction_sync(uow, _write)
        self.reload_source(uow, source)
        return result

    def delete_credential(self, uow: SqlUnitOfWork, source: str, cred_id: int) -> None:
        """Delete one credential owned by *source* and reload *source*."""
        self._require_plugin(source)

        def _write(tx: SqlUnitOfWork) -> None:
            repo = SQLAlchemySourceRepository(tx.session)
            cred = repo.get_credential(cred_id)
            if cred is None or cred.source != source:
                raise NotFoundError(f"Credential {cred_id} not found")
            repo.delete_credential(cred_id)

        run_write_transaction_sync(uow, _write)
        self.reload_source(uow, source)

    def reorder_credentials(
        self,
        uow: SqlUnitOfWork,
        source: str,
        ids: list[int],
    ) -> int:
        """Persist new credential priority ordering for *source* without reloading runtime pools."""
        self._require_plugin(source)
        return run_write_transaction_sync(
            uow,
            lambda tx: SQLAlchemySourceRepository(tx.session).reorder_credentials(source, ids),
        )


__all__ = ["SourceService"]
