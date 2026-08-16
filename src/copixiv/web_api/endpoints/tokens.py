"""Token API endpoints."""

from fastapi import APIRouter, Depends

from copixiv.domain.exceptions import NotFoundError
from copixiv.web_api.deps import get_uow, get_write_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import TokenBase, TokenUpdate

router = APIRouter()


# Route manifest — mounted automatically by the composition root
# (docs/MODULARITY.md §M9): (prefix, tags) travels with the module.
ROUTE = ("/api/tokens", ["tokens"])



def _mask_token(token: str) -> str:
    """Mask a refresh token so only its last 4 characters are visible."""
    if len(token) <= 4:
        return "****"
    return f"****{token[-4:]}"


def _token_to_masked_dict(t) -> dict:
    """Build the v1-compatible token dict with the refresh token masked."""
    return {
        "id": t.id,
        "name": t.name,
        "token": _mask_token(t.token),
        "premium": t.premium,
        "valid": t.valid,
    }


@router.get("/")
async def get_tokens(uow: SqlUnitOfWork = Depends(get_uow)):
    # Build the response manually — a response_model=list[TokenResponse]
    # would serialize the raw refresh_token back to the client.
    return [_token_to_masked_dict(t) for t in await uow.tokens.get_all()]


@router.post("/")
async def create_token(data: TokenBase, uow: SqlUnitOfWork = Depends(get_write_uow)):
    # The request schema still accepts the full token; only the response
    # is masked so the client can't read back the secret it just submitted.
    created = await uow.tokens.create(data.model_dump())
    return _token_to_masked_dict(created)


@router.put("/{token_id}")
async def update_token(token_id: int, data: TokenUpdate, uow: SqlUnitOfWork = Depends(get_write_uow)):
    result = await uow.tokens.update(token_id, data.model_dump(exclude_none=True))
    if result is None:
        raise NotFoundError(f"Token {token_id} not found")
    return _token_to_masked_dict(result)


@router.delete("/{token_id}")
async def delete_token(token_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await uow.tokens.delete(token_id):
        raise NotFoundError(f"Token {token_id} not found")
    return {"ok": True}


@router.post("/reorder/")
async def reorder_tokens(ids: list[int], uow: SqlUnitOfWork = Depends(get_write_uow)):
    await uow.tokens.reorder(ids)
    return {"ok": True}
