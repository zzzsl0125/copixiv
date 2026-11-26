"""Pydantic HTTP request and response schemas for ``/api/sources``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from copixiv.core.sources import (
    CredentialFieldType,
    EpubPolicy,
    IntakeAction,
    SettingFieldType,
)


def _strip_nonempty_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("凭据名称不能为空")
    return stripped


class SettingFieldOptionSchema(BaseModel):
    """Select option entry rendered in source settings UI."""

    value: str
    label: str


class SettingFieldSpecSchema(BaseModel):
    """Schema describing one configurable setting for a source."""

    key: str
    label: str
    field_type: SettingFieldType
    default: Any = None
    description: str = ""
    options: list[SettingFieldOptionSchema] = Field(default_factory=list)
    min_value: float | None = None
    max_value: float | None = None
    visible_when: dict[str, Any] | None = None


class CredentialFieldSpecSchema(BaseModel):
    """Schema describing one secret or metadata field for a source credential."""

    key: str
    label: str
    field_type: CredentialFieldType
    required: bool = False
    default: Any = None
    placeholder: str = ""
    description: str = ""


class CredentialKindSpecSchema(BaseModel):
    """Schema describing one credential kind supported by a source."""

    kind: str
    label: str
    secret_fields: list[CredentialFieldSpecSchema] = Field(default_factory=list)
    meta_fields: list[CredentialFieldSpecSchema] = Field(default_factory=list)
    supports_reorder: bool = False
    unique_meta_flags: list[str] = Field(default_factory=list)


class SourceShelfDefSchema(BaseModel):
    """Sidebar shelf and intake definition advertised by a source."""

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


class SourceManifestSchema(BaseModel):
    """Static capability and UI manifest for a registered source plugin."""

    source: str
    display_name: str
    badge_label: str = ""
    badge_class: str = ""
    capabilities: list[str] = Field(default_factory=list)
    editable_fields: list[str] = Field(default_factory=list)
    epub_policy: EpubPolicy = EpubPolicy.CONTAINER_ONLY
    settings_schema: list[SettingFieldSpecSchema] = Field(default_factory=list)
    credential_kinds: list[CredentialKindSpecSchema] = Field(default_factory=list)
    shelf: SourceShelfDefSchema | None = None
    allow_proxy_config: bool = True
    enabled_by_default: bool = True


class SourceItemResponse(BaseModel):
    """Summary item returned by ``GET /api/sources`` and ``PUT /api/sources/{source}/config``."""

    source: str
    enabled: bool
    config: dict[str, Any]
    manifest: SourceManifestSchema
    credential_count: int = 0
    valid_credential_count: int = 0


class SourceConfigUpdateRequest(BaseModel):
    """Request payload for ``PUT /api/sources/{source}/config``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    config: dict[str, Any] | None = None


class SourceTestResponse(BaseModel):
    """Response payload for ``POST /api/sources/{source}/test``."""

    ok: bool
    latency_ms: int | None = None
    message: str = ""
    account_summary: str | None = None


class SourceCredentialResponse(BaseModel):
    """Credential representation returned by credential CRUD endpoints (secrets masked)."""

    id: int
    source: str
    name: str
    kind: str
    secret: dict[str, Any]
    valid: bool
    sort_index: int
    meta: dict[str, Any]


class SourceCredentialCreateRequest(BaseModel):
    """Request payload for ``POST /api/sources/{source}/credentials``."""

    name: str
    kind: str | None = None
    secret: dict[str, Any] = Field(default_factory=dict)
    valid: bool = True
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        result = _strip_nonempty_name(value)
        assert result is not None
        return result


class SourceCredentialUpdateRequest(BaseModel):
    """Request payload for ``PUT /api/sources/{source}/credentials/{cred_id}``."""

    name: str | None = None
    secret: dict[str, Any] | None = None
    valid: bool | None = None
    meta: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return _strip_nonempty_name(value)


class SourceFetchRequest(BaseModel):
    """Request payload for ``POST /api/sources/fetch``."""

    source: str | None = None
    refs: list[str] = Field(min_length=1)
    force: bool = False


class SourceFetchResponse(BaseModel):
    """Response payload for ``POST /api/sources/fetch``."""

    task_id: int
    source: str
    ref_count: int


__all__ = [
    "CredentialFieldSpecSchema",
    "CredentialKindSpecSchema",
    "SettingFieldOptionSchema",
    "SettingFieldSpecSchema",
    "SourceConfigUpdateRequest",
    "SourceCredentialCreateRequest",
    "SourceCredentialResponse",
    "SourceCredentialUpdateRequest",
    "SourceFetchRequest",
    "SourceFetchResponse",
    "SourceItemResponse",
    "SourceManifestSchema",
    "SourceShelfDefSchema",
    "SourceTestResponse",
]
