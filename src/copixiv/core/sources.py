"""Pure multi-source contracts, manifest schemas, runtime configs, and plugin protocol."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


def normalize_proxy_url(proxy_url: str | None) -> str | None:
    """Normalise a proxy string (``127.0.0.1:20172`` → ``http://127.0.0.1:20172``)."""
    if not proxy_url:
        return None
    cleaned = proxy_url.strip()
    if not cleaned:
        return None
    if "://" not in cleaned:
        return f"http://{cleaned}"
    return cleaned


class SourceCapability(StrEnum):
    """Capabilities declared by a source plugin manifest."""

    FETCH_BY_REF = "fetch_by_ref"
    FILE_UPLOAD = "file_upload"
    SYNC_NOVEL = "sync_novel"
    RETRY_FAILURE = "retry_failure"
    AUTH_POOL = "auth_pool"
    AUTHOR_FEED = "author_feed"
    LOOKUP_ENTITY = "lookup_entity"
    TEST_CONNECTION = "test_connection"


class EpubPolicy(StrEnum):
    """How EPUB reconciliation validates novels belonging to a source."""

    CONTAINER_ONLY = "container_only"
    STRICT_PLACEHOLDERS = "strict_placeholders"
    BEST_EFFORT_EXTERNAL = "best_effort_external"


ProxyMode = Literal["inherit", "direct", "custom"]
SettingFieldType = Literal["string", "number", "boolean", "select", "key_value"]
CredentialFieldType = Literal["string", "secret", "boolean"]
IntakeAction = Literal["none", "file_upload", "ref_fetch"]


class SettingFieldOption(BaseModel):
    """Option entry for a ``select`` setting field."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str


class SettingFieldSpec(BaseModel):
    """Declarative specification for one configurable source setting."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    field_type: SettingFieldType
    default: Any = None
    description: str = ""
    options: tuple[SettingFieldOption, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    visible_when: dict[str, Any] | None = None


class CredentialFieldSpec(BaseModel):
    """Declarative specification for one credential secret or metadata field."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    field_type: CredentialFieldType
    required: bool = False
    default: Any = None
    placeholder: str = ""
    description: str = ""


class CredentialKindSpec(BaseModel):
    """Declarative schema for a credential type accepted by a source."""

    model_config = ConfigDict(frozen=True)

    kind: str
    label: str
    secret_fields: tuple[CredentialFieldSpec, ...] = ()
    meta_fields: tuple[CredentialFieldSpec, ...] = ()
    supports_reorder: bool = False
    unique_meta_flags: tuple[str, ...] = ()


class SourceShelfDef(BaseModel):
    """Sidebar shelf and top-bar intake definition exposed by a source."""

    model_config = ConfigDict(frozen=True)

    section_id: str
    label: str
    keyword: str
    min_like: int = 0
    min_text: int = 0
    order_by: str = "id"
    order_direction: str = "DESC"
    icon: str = "book"
    intake_action: IntakeAction = "none"
    intake_placeholder: str = ""
    intake_button_text: str = ""


class SourceManifest(BaseModel):
    """Static capability, UI schema, and policy manifest of a source plugin."""

    model_config = ConfigDict(frozen=True)

    source: str
    display_name: str
    badge_label: str = ""
    badge_class: str = ""
    capabilities: frozenset[SourceCapability] = frozenset()
    editable_fields: tuple[str, ...] = ()
    epub_policy: EpubPolicy = EpubPolicy.CONTAINER_ONLY
    settings_schema: tuple[SettingFieldSpec, ...] = ()
    credential_kinds: tuple[CredentialKindSpec, ...] = ()
    shelf: SourceShelfDef | None = None
    allow_proxy_config: bool = True

    def has_capability(self, cap: SourceCapability) -> bool:
        """Return whether *cap* is supported by this source."""
        return cap in self.capabilities

    def default_config(self) -> dict[str, Any]:
        """Return a fresh dict of default settings from ``settings_schema``."""
        return {spec.key: copy.deepcopy(spec.default) for spec in self.settings_schema}


class SourceRuntimeConfig(BaseModel):
    """Resolved runtime configuration passed to a source plugin."""

    model_config = ConfigDict(frozen=True)

    source: str
    enabled: bool = True
    proxy_mode: ProxyMode = "inherit"
    proxy_url: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_values(
        cls,
        source: str,
        enabled: bool,
        values: dict[str, Any],
    ) -> SourceRuntimeConfig:
        """Build a :class:`SourceRuntimeConfig` extracting proxy settings from *values*."""
        copied = dict(values)
        raw_mode = copied.get("proxy_mode")
        proxy_mode: ProxyMode = raw_mode if raw_mode in ("inherit", "direct", "custom") else "inherit"
        raw_url = copied.get("proxy_url")
        proxy_url = normalize_proxy_url(raw_url if isinstance(raw_url, str) else None)
        return cls(
            source=source,
            enabled=enabled,
            proxy_mode=proxy_mode,
            proxy_url=proxy_url,
            values=copied,
        )


class SourceCredentialRecord(BaseModel):
    """In-memory representation of a persisted source credential row."""

    model_config = ConfigDict(frozen=True)

    id: int
    source: str
    name: str
    kind: str
    secret: dict[str, Any]
    valid: bool = True
    sort_index: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class SourceTestResult(BaseModel):
    """Result returned by ``SourcePlugin.test_connection``."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    latency_ms: int | None = None
    message: str = ""
    account_summary: str | None = None


class ParsedSourceRef(BaseModel):
    """Normalized book/chapter reference extracted by ``SourcePlugin.parse_ref``."""

    model_config = ConfigDict(frozen=True)

    source: str
    kind: Literal["book", "chapter"]
    series_key: str
    chapter_key: str | None = None
    normalized_ref: str = ""


class ExternalAuthor(BaseModel):
    """Author metadata returned by an external source plugin."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    name: str | None = None


class ExternalSeries(BaseModel):
    """Series/book metadata returned by an external source plugin."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    title: str | None = None
    cover_url: str | None = None
    author: ExternalAuthor | None = None


class ExternalChapterEntry(BaseModel):
    """Table-of-contents chapter entry within an :class:`ExternalBookOutline`."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    title: str
    series_index: int
    is_external_link: bool = False
    external_url: str | None = None


class ExternalChapter(BaseModel):
    """Full fetched chapter/novel payload returned by a source plugin."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    title: str
    series_index: int | None = None
    author: ExternalAuthor | None = None
    create_time: datetime | None = None
    like: int = 0
    view: int = 0
    tags: tuple[str, ...] = ()
    content: str = ""
    images: dict[str, str] = Field(default_factory=dict)
    cover_url: str | None = None
    is_external_link: bool = False


class ExternalBookOutline(BaseModel):
    """Book-level metadata and chapter index returned by ``fetch_book_outline``."""

    model_config = ConfigDict(frozen=True)

    series: ExternalSeries
    chapter_entries: tuple[ExternalChapterEntry, ...] = ()
    author: ExternalAuthor | None = None
    tags: tuple[str, ...] = ()
    like: int = 0
    view: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)


FALLBACK_EDITABLE_FIELDS: tuple[str, ...] = ("series_index", "tags", "create_time")


def make_fallback_manifest(source: str) -> SourceManifest:
    """Create a read-only fallback manifest for an unknown or removed source."""
    return SourceManifest(
        source=source,
        display_name=source,
        badge_label=source.upper(),
        badge_class="bg-gray-100 text-gray-700",
        capabilities=frozenset(),
        editable_fields=FALLBACK_EDITABLE_FIELDS,
        epub_policy=EpubPolicy.CONTAINER_ONLY,
        shelf=None,
        allow_proxy_config=False,
    )


class FallbackSourcePolicy:
    """Policy helper applied when a novel's ``source`` is not registered."""

    editable_fields: tuple[str, ...] = FALLBACK_EDITABLE_FIELDS
    epub_policy: EpubPolicy = EpubPolicy.CONTAINER_ONLY
    can_sync: bool = False

    @staticmethod
    def manifest_for(source: str) -> SourceManifest:
        """Return a fallback :class:`SourceManifest` for *source*."""
        return make_fallback_manifest(source)

    @staticmethod
    def external_url(*_args: Any, **_kwargs: Any) -> None:
        """Unknown sources have no external URL."""
        return None


@runtime_checkable
class SourcePlugin(Protocol):
    """In-process plugin contract implemented by every novel source."""

    manifest: SourceManifest

    def validate_config(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    def parse_ref(self, raw: str) -> ParsedSourceRef | None: ...

    def build_external_url(
        self,
        kind: Literal["novel", "series", "author"],
        source_key: str,
        config: SourceRuntimeConfig,
    ) -> str | None: ...

    async def fetch_book_outline(
        self,
        series_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> ExternalBookOutline: ...

    async def fetch_chapter(
        self,
        book_key: str,
        chapter_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
        *,
        password: str | None = None,
    ) -> ExternalChapter: ...

    async def fetch_novel_for_sync(
        self,
        source_key: str,
        stored_path: str | None,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> ExternalChapter | None: ...

    async def fetch_author_feed(
        self,
        author_source_keys: Sequence[str],
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> list[Any]: ...

    async def on_author_deleted(
        self,
        author_source_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None: ...

    async def test_connection(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> SourceTestResult: ...

    def on_reload(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None: ...


__all__ = [
    "CredentialFieldSpec",
    "CredentialFieldType",
    "CredentialKindSpec",
    "EpubPolicy",
    "ExternalAuthor",
    "ExternalBookOutline",
    "ExternalChapter",
    "ExternalChapterEntry",
    "ExternalSeries",
    "FALLBACK_EDITABLE_FIELDS",
    "FallbackSourcePolicy",
    "IntakeAction",
    "ParsedSourceRef",
    "ProxyMode",
    "SettingFieldOption",
    "SettingFieldSpec",
    "SettingFieldType",
    "SourceCapability",
    "SourceCredentialRecord",
    "SourceManifest",
    "SourcePlugin",
    "SourceRuntimeConfig",
    "SourceShelfDef",
    "SourceTestResult",
    "make_fallback_manifest",
    "normalize_proxy_url",
]
