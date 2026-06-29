"""Single Pixiv account — wraps pixivpy3 AppPixivAPI with auth and rate-limiting."""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum

from pixivpy3 import AppPixivAPI, PixivError

from copixiv.domain.services.parsing import safe_get

logger = logging.getLogger("copixiv")


@dataclass
class TokenInfo:
    token: str
    username: str
    premium: bool = False
    valid: bool = True


@dataclass
class AccountStrategy:
    need_premium: bool = False
    force_account: str | None = None


class AccountStatus(Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    INVALID = "invalid"


class RateLimitError(PixivError):
    """Account is rate-limited."""


class AccountInvalidError(PixivError):
    """Account token is permanently invalid."""


class PixivAccount:
    """A single Pixiv account with authentication and rate limiting.

    Wraps pixivpy3's ``AppPixivAPI``, managing auth state and cooldown
    after rate-limit hits.
    """

    def __init__(
        self,
        token_info: TokenInfo,
        proxy_http: str = "",
        proxy_https: str = "",
        min_interval: float = 2.0,
        cooling_duration: float = 120.0,
    ):
        self.token_info = token_info
        self.api = self._create_api(proxy_http, proxy_https)
        self.status = AccountStatus.INACTIVE
        self.username = token_info.username

        self.last_req_time: float = 0.0
        self._cooldown_until: float = 0.0
        self.min_interval = min_interval
        self.cooling_duration = cooling_duration

        self._auth_lock = asyncio.Lock()
        self._req_lock = asyncio.Lock()

    def __str__(self) -> str:
        return f"[{self.username[:6]}]"

    # -- status properties ---------------------------------------------------

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    @property
    def valid(self) -> bool:
        return self.status != AccountStatus.INVALID

    @property
    def available(self) -> bool:
        if not self.valid or self.in_cooldown:
            return False
        if (
            self.status == AccountStatus.ACTIVE
            and time.time() - self.last_req_time > 3500
        ):
            self.status = AccountStatus.INACTIVE
        return self.status == AccountStatus.ACTIVE

    # -- actions -------------------------------------------------------------

    def start_cooldown(self, duration: float | None = None) -> None:
        self._cooldown_until = time.time() + (duration or self.cooling_duration)

    def _create_api(self, proxy_http: str, proxy_https: str) -> AppPixivAPI:
        proxies = {"http": proxy_http, "https": proxy_https}
        return AppPixivAPI(proxies=proxies)

    async def authenticate(self) -> None:
        if self.status == AccountStatus.INVALID:
            raise AccountInvalidError(str(self))

        if self.status == AccountStatus.ACTIVE:
            return

        async with self._auth_lock:
            try:
                await asyncio.to_thread(
                    self.api.auth, refresh_token=self.token_info.token
                )
                self.status = AccountStatus.ACTIVE
                logger.info(f"{self} 认证成功")
            except PixivError as e:
                if "auth() failed" in str(e).lower():
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(str(self)) from e
                raise

    async def execute(self, method: str, *args, **kwargs):
        """Call an API method on this account, handling auth and rate limits."""
        await self.authenticate()

        # Rate limiting — ensure min_interval between calls
        async with self._req_lock:
            elapsed = time.time() - self.last_req_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)

            try:
                func = getattr(self.api, method)
                result = await asyncio.to_thread(func, *args, **kwargs)
                self.last_req_time = time.time()
                return result
            except PixivError as e:
                error_msg = str(e).lower()
                if "invalid_grant" in error_msg:
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(str(self)) from e
                if "rate limit" in error_msg or "currently restricted" in error_msg:
                    self.start_cooldown()
                    raise RateLimitError(str(self)) from e
                raise
