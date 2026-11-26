"""HTTP API endpoints for ``/api/sources`` (source config, test, and credentials)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from copixiv.core.exceptions import ValidationError
from copixiv.core.sources import SourceCapability
from copixiv.deps import SourceHttpManagerDep, SourceServiceDep, TaskManagerDep, UowDep
from copixiv.features.sources.schemas import (
    SourceConfigUpdateRequest,
    SourceCredentialCreateRequest,
    SourceCredentialResponse,
    SourceCredentialUpdateRequest,
    SourceFetchRequest,
    SourceFetchResponse,
    SourceItemResponse,
    SourceTestResponse,
)

router = APIRouter()


@router.get("", response_model=list[SourceItemResponse])
def list_sources(uow: UowDep, source_service: SourceServiceDep):
    """List all registered sources with runtime configs, manifests, and credential counts."""
    with uow.begin(read_only=True):
        return source_service.list_sources(uow.session)


@router.post("/fetch", response_model=SourceFetchResponse)
def fetch_by_refs(
    data: SourceFetchRequest,
    source_service: SourceServiceDep,
    task_manager: TaskManagerDep,
):
    """Validate source references and enqueue a ``source_fetch`` background task."""
    cleaned_refs = [ref.strip() for ref in data.refs if isinstance(ref, str) and ref.strip()]
    if not cleaned_refs:
        raise ValidationError("请提供至少一条有效引用")

    preferred_source = data.source.strip() if isinstance(data.source, str) else None
    if preferred_source == "":
        preferred_source = None
    if preferred_source is not None:
        pref_plugin = source_service.require_enabled(preferred_source)
        if not pref_plugin.manifest.has_capability(SourceCapability.FETCH_BY_REF):
            raise ValidationError(f"数据源 {pref_plugin.manifest.display_name} 不支持按引用抓取")

    normalized_refs: list[str] = []
    resolved_sources: list[str] = []
    checked_sources: set[str] = set()
    for ref in cleaned_refs:
        parsed = source_service.registry.parse_ref(ref, preferred_source=preferred_source)
        if parsed is None:
            raise ValidationError(f"无法识别的引用: {ref}")
        if parsed.source not in checked_sources:
            plugin = source_service.require_enabled(parsed.source)
            if not plugin.manifest.has_capability(SourceCapability.FETCH_BY_REF):
                raise ValidationError(f"数据源 {plugin.manifest.display_name} 不支持按引用抓取")
            checked_sources.add(parsed.source)
        resolved_sources.append(parsed.source)
        normalized_refs.append(parsed.normalized_ref or ref)

    unique_sources = list(dict.fromkeys(resolved_sources))
    if len(unique_sources) == 1:
        resolved_source_or_none: str | None = unique_sources[0]
        resolved_source_label = unique_sources[0]
    else:
        resolved_source_or_none = preferred_source
        resolved_source_label = preferred_source or "mixed"

    task_id = task_manager.run_task(
        "source_fetch",
        {
            "source": resolved_source_or_none,
            "refs": normalized_refs,
            "force": data.force,
        },
    )
    return SourceFetchResponse(
        task_id=task_id,
        source=resolved_source_label,
        ref_count=len(normalized_refs),
    )


@router.put("/{source}/config", response_model=SourceItemResponse)
async def update_source_config(
    source: str,
    data: SourceConfigUpdateRequest,
    uow: UowDep,
    source_service: SourceServiceDep,
    http_manager: SourceHttpManagerDep,
):
    """Update a source's enabled flag or settings and hot-reload its runtime."""
    try:
        return await asyncio.to_thread(
            source_service.update_config,
            uow,
            source,
            enabled=data.enabled,
            config=data.config,
        )
    finally:
        await http_manager.aclose_retired()


@router.post("/{source}/test", response_model=SourceTestResponse)
async def test_source_connection(
    source: str,
    uow: UowDep,
    source_service: SourceServiceDep,
):
    """Run an active connectivity check against *source*."""
    result = await source_service.test_connection(uow, source)
    return SourceTestResponse.model_validate(result.model_dump())


@router.get("/{source}/credentials", response_model=list[SourceCredentialResponse])
def list_source_credentials(
    source: str,
    uow: UowDep,
    source_service: SourceServiceDep,
):
    """List masked credentials configured for *source*."""
    return source_service.list_credentials_masked(uow, source)


@router.post("/{source}/credentials", response_model=SourceCredentialResponse)
async def create_source_credential(
    source: str,
    data: SourceCredentialCreateRequest,
    uow: UowDep,
    source_service: SourceServiceDep,
    http_manager: SourceHttpManagerDep,
):
    """Create a new credential for *source* and hot-reload its runtime."""
    try:
        return await asyncio.to_thread(
            source_service.create_credential,
            uow,
            source,
            name=data.name,
            kind=data.kind,
            secret=data.secret,
            valid=data.valid,
            meta=data.meta,
        )
    finally:
        await http_manager.aclose_retired()


@router.put("/{source}/credentials/{cred_id}", response_model=SourceCredentialResponse)
async def update_source_credential(
    source: str,
    cred_id: int,
    data: SourceCredentialUpdateRequest,
    uow: UowDep,
    source_service: SourceServiceDep,
    http_manager: SourceHttpManagerDep,
):
    """Update an existing credential for *source* and hot-reload its runtime."""
    try:
        return await asyncio.to_thread(
            source_service.update_credential,
            uow,
            source,
            cred_id,
            name=data.name,
            secret=data.secret,
            valid=data.valid,
            meta=data.meta,
        )
    finally:
        await http_manager.aclose_retired()


@router.delete("/{source}/credentials/{cred_id}", status_code=204)
async def delete_source_credential(
    source: str,
    cred_id: int,
    uow: UowDep,
    source_service: SourceServiceDep,
    http_manager: SourceHttpManagerDep,
):
    """Delete a credential from *source* and hot-reload its runtime."""
    try:
        await asyncio.to_thread(
            source_service.delete_credential,
            uow,
            source,
            cred_id,
        )
    finally:
        await http_manager.aclose_retired()


@router.post("/{source}/credentials/reorder")
def reorder_source_credentials(
    source: str,
    ids: list[int],
    uow: UowDep,
    source_service: SourceServiceDep,
):
    """Reorder credential priority for *source* without triggering runtime reload."""
    source_service.reorder_credentials(uow, source, ids)
    return {"ok": True}
