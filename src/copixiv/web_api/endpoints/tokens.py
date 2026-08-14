"""Token API endpoints."""

from fastapi import APIRouter, Depends

from copixiv.domain.exceptions import NotFoundError
from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import TokenBase, TokenUpdate, TokenResponse

router = APIRouter()


@router.get("/", response_model=list[TokenResponse])
async def get_tokens(uow: SqlUnitOfWork = Depends(get_uow)):
    return await uow.tokens.get_all()


@router.post("/", response_model=TokenResponse)
async def create_token(data: TokenBase, uow: SqlUnitOfWork = Depends(get_uow)):
    return await uow.tokens.create(data.model_dump())


@router.put("/{token_id}", response_model=TokenResponse)
async def update_token(token_id: int, data: TokenUpdate, uow: SqlUnitOfWork = Depends(get_uow)):
    result = await uow.tokens.update(token_id, data.model_dump(exclude_none=True))
    if result is None:
        raise NotFoundError(f"Token {token_id} not found")
    return result


@router.delete("/{token_id}")
async def delete_token(token_id: int, uow: SqlUnitOfWork = Depends(get_uow)):
    if not await uow.tokens.delete(token_id):
        raise NotFoundError(f"Token {token_id} not found")
    return {"ok": True}


@router.post("/reorder/")
async def reorder_tokens(ids: list[int], uow: SqlUnitOfWork = Depends(get_uow)):
    await uow.tokens.reorder(ids)
    return {"ok": True}
