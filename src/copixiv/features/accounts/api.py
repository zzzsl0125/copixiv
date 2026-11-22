"""Token API endpoints."""

import threading

from fastapi import APIRouter
from pydantic import BaseModel, field_validator

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import AccountPoolDep, UowDep
from copixiv.features.accounts.repo import SQLAlchemyTokenRepository

# ---------------------------------------------------------------------------
# Token schemas — carried with the feature (S1 pilot).
# ---------------------------------------------------------------------------


def _strip_nonempty_token(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("Token 不能为空")
    return stripped


class TokenBase(BaseModel):
    name: str
    token: str
    premium: bool = False
    valid: bool = True
    # Designated「追更账号」—— the account that owns the Pixiv following feed.
    is_follow: bool = False

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        result = _strip_nonempty_token(value)
        assert result is not None
        return result


class TokenUpdate(BaseModel):
    name: str | None = None
    token: str | None = None
    premium: bool | None = None
    valid: bool | None = None
    is_follow: bool | None = None

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str | None) -> str | None:
        return _strip_nonempty_token(value)


class TokenResponse(TokenBase):
    id: int


router = APIRouter()
_POOL_SYNC_LOCK = threading.Lock()


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


@router.get("", response_model=list[TokenResponse])
def get_tokens(uow: UowDep):
    # Build each row manually so the masked token (not the raw refresh_token)
    # is what the response model validates and serializes.
    with uow.begin(read_only=True):
        return [_token_to_masked_dict(t) for t in SQLAlchemyTokenRepository(uow.session).get_all()]


def _snapshot_tokens(repo: SQLAlchemyTokenRepository) -> list[dict]:
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


def _sync_account_pool(uow: SqlUnitOfWork, account_pool) -> None:
    """Serialize pool reloads from latest committed DB state."""
    with _POOL_SYNC_LOCK:
        with uow.begin(read_only=True):
            tokens = _snapshot_tokens(SQLAlchemyTokenRepository(uow.session))
        account_pool.reload_from_tokens(tokens)


@router.post("", response_model=TokenResponse)
def create_token(
    data: TokenBase,
    uow: UowDep,
    account_pool: AccountPoolDep,
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
        return _token_to_masked_dict(created)

    res = run_write_transaction_sync(uow, _write)
    _sync_account_pool(uow, account_pool)
    return res


@router.put("/{token_id}", response_model=TokenResponse)
def update_token(
    token_id: int,
    data: TokenUpdate,
    uow: UowDep,
    account_pool: AccountPoolDep,
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
            repo.set_follow(token_id, bool(follow_val))
            result.is_follow = bool(follow_val)
        return _token_to_masked_dict(result)

    res = run_write_transaction_sync(uow, _write)
    _sync_account_pool(uow, account_pool)
    return res


@router.delete("/{token_id}", status_code=204)
def delete_token(
    token_id: int,
    uow: UowDep,
    account_pool: AccountPoolDep,
):
    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTokenRepository(tx.session)
        if not repo.delete(token_id):
            raise NotFoundError(f"Token {token_id} not found")

    run_write_transaction_sync(uow, _write)
    _sync_account_pool(uow, account_pool)


@router.post("/reorder")
def reorder_tokens(
    ids: list[int],
    uow: UowDep,
):
    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTokenRepository(tx.session)
        repo.reorder(ids)

    run_write_transaction_sync(uow, _write)
    return {"ok": True}
