"""Token API endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.web_api.schemas import TokenCreate, TokenUpdate, TokenResponse
from copixiv.infrastructure.repositories.token import TokenRepository
from copixiv.application.token import (
    ListTokensUseCase, CreateTokenUseCase, UpdateTokenUseCase,
    DeleteTokenUseCase, ReorderTokensUseCase,
)

router = APIRouter()


@router.get("/", response_model=list[TokenResponse])
async def get_tokens(db: Session = Depends(get_db)):
    use_case = ListTokensUseCase(TokenRepository(db))
    return await use_case.execute()


@router.post("/", response_model=TokenResponse)
async def create_token(data: TokenCreate, db: Session = Depends(get_db)):
    use_case = CreateTokenUseCase(TokenRepository(db))
    result = await use_case.execute(data.model_dump())
    db.commit()
    return result


@router.put("/{token_id}", response_model=TokenResponse)
async def update_token(token_id: int, data: TokenUpdate, db: Session = Depends(get_db)):
    use_case = UpdateTokenUseCase(TokenRepository(db))
    result = await use_case.execute(token_id, data.model_dump(exclude_none=True))
    db.commit()
    return result


@router.delete("/{token_id}")
async def delete_token(token_id: int, db: Session = Depends(get_db)):
    use_case = DeleteTokenUseCase(TokenRepository(db))
    await use_case.execute(token_id)
    db.commit()
    return {"ok": True}


@router.post("/reorder/")
async def reorder_tokens(ids: list[int], db: Session = Depends(get_db)):
    use_case = ReorderTokensUseCase(TokenRepository(db))
    await use_case.execute(ids)
    db.commit()
    return {"ok": True}
