"""Token API endpoints."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import run_write_transaction_sync
from copixiv.deps import get_account_pool, get_uow
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


@router.get("/", response_model=list[TokenResponse])
def get_tokens(uow: SqlUnitOfWork = Depends(get_uow)):
    # Build each row manually so the masked token (not the raw refresh_token)
    # is what the response model validates and serializes.
    with uow.begin():
        return [
            _token_to_masked_dict(t)
            for t in SQLAlchemyTokenRepository(uow.session).get_all()
        ]


def _snapshot_tokens(repo: SQLAlchemyTokenRepository) -> list[dict]:
    repo.session.flush()
    return [
        {
            "name": t.name,
            "token": t.token,
            "premium": t.premium,
            "valid": t.valid,
            "is_follow": bool(t.is_follow),
        }
        for t in repo.get_all()
    ]


@router.post("/", response_model=TokenResponse)
def create_token(
    data: TokenBase,
    uow: SqlUnitOfWork = Depends(get_uow),
    account_pool=Depends(get_account_pool),
):
    # The request schema still accepts the full token; only the response
    # is masked so the client can't read back the secret it just submitted.
    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTokenRepository(tx.session)
        payload = data.model_dump()
        want_follow = bool(payload.pop("is_follow", False))
        created = repo.create(payload)
        if want_follow:
            repo.set_follow(created.id, True)
            created.is_follow = True
        return _token_to_masked_dict(created), _snapshot_tokens(repo)

    res, tokens = run_write_transaction_sync(uow, _write)
    account_pool.reload_from_tokens(tokens)
    return res


@router.put("/{token_id}", response_model=TokenResponse)
def update_token(
    token_id: int,
    data: TokenUpdate,
    uow: SqlUnitOfWork = Depends(get_uow),
    account_pool=Depends(get_account_pool),
):
    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTokenRepository(tx.session)
        payload = data.model_dump(exclude_none=True)
        # is_follow is a singleton designation — clear it on every other
        # account before setting it on this one (see the token repository).
        follow_val = payload.pop("is_follow", None)
        result = repo.update(token_id, payload)
        if result is None:
            raise NotFoundError(f"Token {token_id} not found")
        if follow_val is not None:
            if not repo.set_follow(token_id, bool(follow_val)):
                raise NotFoundError(f"Token {token_id} not found")
            result.is_follow = bool(follow_val)
        return _token_to_masked_dict(result), _snapshot_tokens(repo)

    res, tokens = run_write_transaction_sync(uow, _write)
    account_pool.reload_from_tokens(tokens)
    return res


@router.delete("/{token_id}")
def delete_token(
    token_id: int,
    uow: SqlUnitOfWork = Depends(get_uow),
    account_pool=Depends(get_account_pool),
):
    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTokenRepository(tx.session)
        if not repo.delete(token_id):
            raise NotFoundError(f"Token {token_id} not found")
        return _snapshot_tokens(repo)

    tokens = run_write_transaction_sync(uow, _write)
    account_pool.reload_from_tokens(tokens)
    return {"ok": True}


@router.post("/reorder/")
def reorder_tokens(
    ids: list[int],
    uow: SqlUnitOfWork = Depends(get_uow),
    account_pool=Depends(get_account_pool),
):
    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTokenRepository(tx.session)
        repo.reorder(ids)
        return _snapshot_tokens(repo)

    tokens = run_write_transaction_sync(uow, _write)
    account_pool.reload_from_tokens(tokens)
    return {"ok": True}
