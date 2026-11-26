"""Default base implementation for in-process source plugins."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from copixiv.core.exceptions import ValidationError
from copixiv.core.sources import (
    ExternalBookOutline,
    ExternalChapter,
    ParsedSourceRef,
    SettingFieldSpec,
    SourceCredentialRecord,
    SourceManifest,
    SourceRuntimeConfig,
    SourceTestResult,
    normalize_proxy_url,
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_VALID_PROXY_MODES = frozenset({"inherit", "direct", "custom"})


def _coerce_number(spec: SettingFieldSpec, value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValidationError(f"{spec.label} 必须是数字")
    num: int | float
    if isinstance(value, (int, float)):
        num = value
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValidationError(f"{spec.label} 必须是数字")
        try:
            num = int(cleaned)
        except ValueError:
            try:
                num = float(cleaned)
            except ValueError as exc:
                raise ValidationError(f"{spec.label} 必须是有效数字") from exc
    else:
        raise ValidationError(f"{spec.label} 必须是数字")

    if not math.isfinite(num):
        raise ValidationError(f"{spec.label} 必须是有限数字")
    if spec.min_value is not None and num < spec.min_value:
        raise ValidationError(f"{spec.label} 不能小于 {spec.min_value}")
    if spec.max_value is not None and num > spec.max_value:
        raise ValidationError(f"{spec.label} 不能大于 {spec.max_value}")
    if (
        isinstance(num, float)
        and isinstance(spec.default, int)
        and not isinstance(spec.default, bool)
        and num.is_integer()
    ):
        return int(num)
    return num


def _coerce_boolean(spec: SettingFieldSpec, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
    raise ValidationError(f"{spec.label} 必须是布尔值")


def _coerce_setting(spec: SettingFieldSpec, value: Any) -> Any:
    if value is None:
        return copy.deepcopy(spec.default)

    ftype = spec.field_type
    if ftype == "string":
        if not isinstance(value, str):
            raise ValidationError(f"{spec.label} 必须是字符串")
        return value.strip()
    if ftype == "number":
        return _coerce_number(spec, value)
    if ftype == "boolean":
        return _coerce_boolean(spec, value)
    if ftype == "select":
        if not isinstance(value, str):
            raise ValidationError(f"{spec.label} 必须是选项字符串")
        allowed = {opt.value for opt in spec.options}
        if allowed and value not in allowed:
            raise ValidationError(f"{spec.label} 的选项无效：{value!r}")
        return value
    if ftype == "key_value":
        if not isinstance(value, Mapping):
            raise ValidationError(f"{spec.label} 必须是键值映射")
        cleaned: dict[str, str] = {}
        for raw_key, raw_val in value.items():
            if not isinstance(raw_key, str) or not isinstance(raw_val, str):
                raise ValidationError(f"{spec.label} 的键和值必须均为字符串")
            key = raw_key.strip()
            if not key:
                raise ValidationError(f"{spec.label} 中存在空键名")
            cleaned[key] = raw_val
        return cleaned
    raise ValidationError(f"不支持的配置字段类型：{ftype}")


class BaseSourcePlugin:
    """Shared base class implementing default :class:`SourcePlugin` behaviour."""

    manifest: SourceManifest

    def validate_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate, coerce, and fill defaults for *raw* against ``manifest.settings_schema``."""
        if raw is None:
            data: dict[str, Any] = {}
        elif isinstance(raw, Mapping):
            data = dict(raw)
        else:
            raise ValidationError("配置必须是字典对象")

        validated: dict[str, Any] = {}
        for spec in self.manifest.settings_schema:
            raw_val = data.get(spec.key) if spec.key in data else copy.deepcopy(spec.default)
            validated[spec.key] = _coerce_setting(spec, raw_val)

        if self.manifest.allow_proxy_config:
            if "proxy_mode" not in validated and "proxy_mode" in data:
                mode = data["proxy_mode"]
                if not isinstance(mode, str) or mode not in _VALID_PROXY_MODES:
                    raise ValidationError("代理模式无效，仅支持 inherit / direct / custom")
                validated["proxy_mode"] = mode
            if "proxy_url" not in validated and "proxy_url" in data:
                raw_url = data["proxy_url"]
                if raw_url is not None and not isinstance(raw_url, str):
                    raise ValidationError("代理地址必须是字符串")
                validated["proxy_url"] = raw_url.strip() if isinstance(raw_url, str) else ""

        if "proxy_mode" in validated:
            mode = validated["proxy_mode"]
            if mode not in _VALID_PROXY_MODES:
                raise ValidationError("代理模式无效，仅支持 inherit / direct / custom")
            raw_proxy = validated.get("proxy_url")
            normalized = normalize_proxy_url(raw_proxy if isinstance(raw_proxy, str) else None)
            if mode == "custom" and not normalized:
                raise ValidationError("自定义代理模式下必须填写代理地址")
            if "proxy_url" in validated:
                validated["proxy_url"] = normalized or ""

        return validated

    def parse_ref(self, raw: str) -> ParsedSourceRef | None:
        """Parse a user-supplied URL or ID reference; defaults to unsupported."""
        del raw
        return None

    def build_external_url(
        self,
        kind: Literal["novel", "series", "author"],
        source_key: str,
        config: SourceRuntimeConfig,
    ) -> str | None:
        """Return an external origin URL for an entity; defaults to ``None``."""
        del kind, source_key, config
        return None

    async def fetch_book_outline(
        self,
        series_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> ExternalBookOutline:
        """Fetch a multi-chapter book outline (implemented by book/chapter web sources)."""
        del series_key, runtime, credentials
        raise NotImplementedError(f"{self.manifest.source} does not support fetch_book_outline")

    async def fetch_chapter(
        self,
        book_key: str,
        chapter_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
        *,
        password: str | None = None,
    ) -> ExternalChapter:
        """Fetch a single chapter payload (implemented by remote chapter sources)."""
        del book_key, chapter_key, runtime, credentials, password
        raise NotImplementedError(f"{self.manifest.source} does not support fetch_chapter")

    async def fetch_novel_for_sync(
        self,
        source_key: str,
        stored_path: str | None,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> ExternalChapter | None:
        """Re-fetch authoritative metadata/content for single-novel sync."""
        del source_key, stored_path, runtime, credentials
        raise NotImplementedError(f"{self.manifest.source} does not support fetch_novel_for_sync")

    async def fetch_author_feed(
        self,
        author_source_keys: Sequence[str],
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> list[Any]:
        """Fetch recent works for followed authors (implemented by ``AUTHOR_FEED`` sources)."""
        del author_source_keys, runtime, credentials
        raise NotImplementedError(f"{self.manifest.source} does not support fetch_author_feed")

    async def on_author_deleted(
        self,
        author_source_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None:
        """Optional remote hook invoked when a local author row is deleted."""
        del author_source_key, runtime, credentials
        return None

    async def test_connection(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> SourceTestResult:
        """Verify source connectivity; defaults to offline-ready success."""
        del runtime, credentials
        return SourceTestResult(ok=True, message="无需网络连接")

    def on_reload(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None:
        """Optional synchronous hook invoked when config or credentials change."""
        del runtime, credentials
        return None


__all__ = ["BaseSourcePlugin"]
