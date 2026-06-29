"""Token API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.web_api.schemas import TokenCreate, TokenUpdate, TokenResponse
from copixiv.infrastructure.repositories.token import TokenRepository

router = APIRouter()


@router.get("/", response_model=list[TokenResponse])
async def get_tokens(db: Session = Depends(get_db)):
    repo = TokenRepository(db)
    return await repo.get_all()


@router.post("/", response_model=TokenResponse)
async def create_token(data: TokenCreate, db: Session = Depends(get_db)):
    repo = TokenRepository(db)
    return await repo.create(data.model_dump())


@router.put("/{token_id}", response_model=TokenResponse)
async def update_token(token_id: int, data: TokenUpdate, db: Session = Depends(get_db)):
    repo = TokenRepository(db)
    result = await repo.update(token_id, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404)
    return result


@router.delete("/{token_id}")
async def delete_token(token_id: int, db: Session = Depends(get_db)):
    repo = TokenRepository(db)
    if not await repo.delete(token_id):
        raise HTTPException(status_code=404)
    return {"ok": True}
