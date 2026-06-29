"""Token domain entity — Pixiv refresh token."""

from pydantic import BaseModel


class Token(BaseModel):
    """A Pixiv refresh token stored for account authentication."""

    id: int = 0
    name: str
    token: str
    premium: bool = False
    valid: bool = True
    sort_index: int = 0
