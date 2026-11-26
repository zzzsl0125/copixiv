"""HTTP API endpoints for ``/api/sources`` (source config, test, and credentials)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from copixiv.deps import SourceHttpManagerDep, SourceServiceDep, UowDep
from copixiv.features.sources.schemas import (
    SourceConfigUpdateRequest,
    SourceCredentialCreateRequest,
    SourceCredentialResponse,
    SourceCredentialUpdateRequest,
    SourceItemResponse,
    SourceTestResponse,
)

router = APIRouter()


@router.get("", response_model=list[SourceItemResponse])
def list_sources(uow: UowDep, source_service: SourceServiceDep):
    """List all registered sources with runtime configs, manifests, and credential counts."""
    with uow.begin(read_only=True):
        return source_service.list_sources(uow.session)


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
