"""Token API endpoints."""

from fastapi import APIRouter, Depends

from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import TokenCreate, TokenUpdate, TokenResponse
from copixiv.application.token import (
    ListTokensUseCase, CreateTokenUseCase, UpdateTokenUseCase,
    DeleteTokenUseCase, ReorderTokensUseCase,
)

router = APIRouter()


@router.get("/", response_model=list[TokenResponse])
async def get_tokens(uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = ListTokensUseCase(uow.tokens)
    return await use_case.execute()


@router.post("/", response_model=TokenResponse)
async def create_token(data: TokenCreate, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = CreateTokenUseCase(uow.tokens)
    result = await use_case.execute(data.model_dump())
    return result


@router.put("/{token_id}", response_model=TokenResponse)
async def update_token(token_id: int, data: TokenUpdate, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = UpdateTokenUseCase(uow.tokens)
    result = await use_case.execute(token_id, data.model_dump(exclude_none=True))
    return result


@router.delete("/{token_id}")
async def delete_token(token_id: int, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = DeleteTokenUseCase(uow.tokens)
    await use_case.execute(token_id)
    return {"ok": True}


@router.post("/reorder/")
async def reorder_tokens(ids: list[int], uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = ReorderTokensUseCase(uow.tokens)
    await use_case.execute(ids)
    return {"ok": True}
