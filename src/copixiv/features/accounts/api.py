"""Token API endpoints."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError
from copixiv.deps import get_uow, get_write_uow
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.accounts.repo import SQLAlchemyTokenRepository


# ---------------------------------------------------------------------------
# Token schemas — carried with the feature (S1 pilot).
# ---------------------------------------------------------------------------

class TokenBase(BaseModel):
    name: str
    token: str
    premium: bool = False
    valid: bool = True
    # Designated「追更账号」—— the account that owns the Pixiv following feed.
    is_follow: bool = False


class TokenUpdate(BaseModel):
    name: str | None = None
    token: str | None = None
    premium: bool | None = None
    valid: bool | None = None
    is_follow: bool | None = None


class TokenResponse(TokenBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


router = APIRouter()


def _mask_token(token: str) -> str:
    """Mask a refresh token so only its last 4 characters are visible."""
    if len(token) <= 4:
        return "****"
    return f"****{token[-4:]}"


def _token_to_masked_dict(t) -> dict:
    """Build the API token dict with the refresh token masked."""
    return {
        "id": t.id,
        "name": t.name,
        "token": _mask_token(t.token),
        "premium": t.premium,
        "valid": t.valid,
        "is_follow": bool(t.is_follow),
    }


@router.get("/")
async def get_tokens(uow: SqlUnitOfWork = Depends(get_uow)):
    # Build the response manually — a response_model=list[TokenResponse]
    # would serialize the raw refresh_token back to the client.
    return [_token_to_masked_dict(t) for t in await SQLAlchemyTokenRepository(uow.session).get_all()]


@router.post("/")
async def create_token(data: TokenBase, uow: SqlUnitOfWork = Depends(get_write_uow)):
    # The request schema still accepts the full token; only the response
    # is masked so the client can't read back the secret it just submitted.
    payload = data.model_dump()
    want_follow = bool(payload.pop("is_follow", False))
    created = await SQLAlchemyTokenRepository(uow.session).create(payload)
    if want_follow:
        await SQLAlchemyTokenRepository(uow.session).set_follow(created.id, True)
        created.is_follow = True
    return _token_to_masked_dict(created)


@router.put("/{token_id}")
async def update_token(token_id: int, data: TokenUpdate, uow: SqlUnitOfWork = Depends(get_write_uow)):
    payload = data.model_dump(exclude_none=True)
    # is_follow is a singleton designation — clear it on every other
    # account before setting it on this one (see the token repository).
    follow_val = payload.pop("is_follow", None)
    result = await SQLAlchemyTokenRepository(uow.session).update(token_id, payload)
    if result is None:
        raise NotFoundError(f"Token {token_id} not found")
    if follow_val is not None:
        if not await SQLAlchemyTokenRepository(uow.session).set_follow(token_id, bool(follow_val)):
            raise NotFoundError(f"Token {token_id} not found")
        result.is_follow = bool(follow_val)
    return _token_to_masked_dict(result)


@router.delete("/{token_id}")
async def delete_token(token_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await SQLAlchemyTokenRepository(uow.session).delete(token_id):
        raise NotFoundError(f"Token {token_id} not found")
    return {"ok": True}


@router.post("/reorder/")
async def reorder_tokens(ids: list[int], uow: SqlUnitOfWork = Depends(get_write_uow)):
    await SQLAlchemyTokenRepository(uow.session).reorder(ids)
    return {"ok": True}
