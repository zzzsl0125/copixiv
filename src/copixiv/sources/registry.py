"""SourceRegistry — ordered registry and policy lookup for source plugins."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Literal

from copixiv.core.sources import (
    EpubPolicy,
    FallbackSourcePolicy,
    ParsedSourceRef,
    SourceCapability,
    SourceManifest,
    SourcePlugin,
    SourceRuntimeConfig,
)


class SourceRegistry:
    """Ordered registry of active :class:`SourcePlugin` instances."""

    def __init__(self, plugins: Iterable[SourcePlugin] = ()) -> None:
        self._plugins: dict[str, SourcePlugin] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: SourcePlugin) -> None:
        """Register a source plugin keyed by ``plugin.manifest.source``."""
        source = plugin.manifest.source
        if not source:
            raise ValueError("Source plugin manifest.source must not be empty")
        self._plugins[source] = plugin

    def get(self, source: str) -> SourcePlugin | None:
        """Return the registered plugin for *source*, or ``None`` if unknown."""
        return self._plugins.get(source)

    def all_plugins(self) -> list[SourcePlugin]:
        """Return all registered plugins in registration order."""
        return list(self._plugins.values())

    def sources(self) -> list[str]:
        """Return all registered source identifiers in registration order."""
        return list(self._plugins.keys())

    def get_manifest_or_fallback(self, source: str) -> SourceManifest:
        """Return the plugin manifest for *source*, or a read-only fallback manifest."""
        plugin = self._plugins.get(source)
        if plugin is not None:
            return plugin.manifest
        return FallbackSourcePolicy.manifest_for(source)

    def editable_fields(self, source: str) -> tuple[str, ...]:
        """Return the editable metadata fields owned by *source*."""
        return self.get_manifest_or_fallback(source).editable_fields

    def epub_policy(self, source: str) -> EpubPolicy:
        """Return the EPUB reconciliation policy for *source*."""
        return self.get_manifest_or_fallback(source).epub_policy

    def has_capability(self, source: str, cap: SourceCapability) -> bool:
        """Return whether *source* is registered and declares capability *cap*."""
        plugin = self._plugins.get(source)
        return plugin.manifest.has_capability(cap) if plugin is not None else False

    def build_external_url(
        self,
        source: str,
        kind: Literal["novel", "series", "author"],
        source_key: str,
        config: SourceRuntimeConfig,
    ) -> str | None:
        """Build an external URL via *source*'s plugin, or ``None`` if unregistered."""
        plugin = self._plugins.get(source)
        if plugin is None:
            return None
        return plugin.build_external_url(kind, source_key, config)

    @staticmethod
    def _invoke_parse_ref(
        plugin: SourcePlugin,
        cleaned: str,
        *,
        allow_bare_id: bool,
    ) -> ParsedSourceRef | None:
        params = inspect.signature(plugin.parse_ref).parameters
        if "allow_bare_id" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return plugin.parse_ref(cleaned, allow_bare_id=allow_bare_id)  # type: ignore[call-arg]
        result = plugin.parse_ref(cleaned)
        if result is not None and not allow_bare_id and cleaned.isdigit():
            return None
        return result

    def parse_ref(
        self,
        raw: str,
        *,
        preferred_source: str | None = None,
    ) -> ParsedSourceRef | None:
        """Resolve a URL or prefixed/bare ID reference via registered plugins."""
        if not raw:
            return None
        cleaned = raw.strip()
        if not cleaned:
            return None

        if preferred_source is not None:
            preferred = self._plugins.get(preferred_source)
            if preferred is not None:
                parsed = self._invoke_parse_ref(preferred, cleaned, allow_bare_id=True)
                if parsed is not None:
                    return parsed

        for source, plugin in self._plugins.items():
            if source == preferred_source:
                continue
            parsed = self._invoke_parse_ref(plugin, cleaned, allow_bare_id=False)
            if parsed is not None:
                return parsed

        return None


__all__ = ["SourceRegistry"]
