"""Single Pixiv account — wraps pixivpy3 AppPixivAPI with auth and rate-limiting."""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum

from pixivpy3 import AppPixivAPI, PixivError

from copixiv.domain.services.parsing import safe_get

from copixiv.log import logger


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


def _fmt_args(args: tuple, kwargs: dict) -> str:
    """Format API call arguments for logging — compact, one-line."""
    parts = [str(a) for a in args]
    parts.extend(f"{k}={v!r}" for k, v in kwargs.items())
    joined = ", ".join(parts)
    return joined if len(joined) <= 120 else joined[:117] + "..."


class RateLimitError(PixivError):
    """Account is rate-limited."""


class AccountInvalidError(PixivError):
    """Account token is permanently invalid."""


class PixivApiError(PixivError):
    """Generic Pixiv API error (neither auth-failure nor rate-limit).

    ``account.execute()`` translates raw pixivpy3 ``PixivError`` instances
    into this type so that modules outside the pixivpy3 ACL (see
    docs/MODULARITY.md §3.2) — e.g. ``client.py`` — never import
    pixivpy3 exception types directly.
    """


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

        # last_req_time is for LRU account selection — set by select()
        # to mark the account as "reserved" so concurrent callers skip it.
        self.last_req_time: float = 0.0
        # _last_call_end is for per-account rate limiting — set after
        # each API call completes so the next call on this account
        # respects min_interval.
        self._last_call_end: float = 0.0
        self._cooldown_until: float = 0.0
        self.min_interval = min_interval
        self.cooling_duration = cooling_duration

        # Track when the last successful auth() happened so we can
        # re-authenticate before the Pixiv access token expires (~1 h).
        self._last_auth_time: float = 0.0

        self._auth_lock = asyncio.Lock()
        self._req_lock = asyncio.Lock()

    # Access tokens from Pixiv typically expire after 1 hour.
    # Re-authenticate if the last auth was more than 50 minutes ago
    # to avoid using an expired token.
    _AUTH_TTL: float = 50 * 60  # 50 minutes

    # An ACTIVE account that hasn't been selected for this long is
    # treated as idle again (status → INACTIVE) so it re-enters the
    # auth + LRU path instead of being considered perpetually fresh.
    _IDLE_TIMEOUT: float = 3500  # seconds (~58 min)

    def __str__(self) -> str:
        return f"[{self.username[:6]}]"

    # -- status properties ---------------------------------------------------

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def valid(self) -> bool:
        return self.status != AccountStatus.INVALID

    @property
    def available(self) -> bool:
        if not self.valid or self.in_cooldown:
            return False
        if (
            self.status == AccountStatus.ACTIVE
            and time.monotonic() - self.last_req_time > self._IDLE_TIMEOUT
        ):
            self.status = AccountStatus.INACTIVE
        return self.status == AccountStatus.ACTIVE

    # -- actions -------------------------------------------------------------

    def start_cooldown(self, duration: float | None = None) -> None:
        self._cooldown_until = time.monotonic() + (
            self.cooling_duration if duration is None else duration
        )

    def _create_api(self, proxy_http: str, proxy_https: str) -> AppPixivAPI:
        proxies = {"http": proxy_http, "https": proxy_https}
        return AppPixivAPI(proxies=proxies, timeout=30)

    async def authenticate(self) -> None:
        if self.status == AccountStatus.INVALID:
            raise AccountInvalidError(str(self))

        # If we authenticated recently the access token is still valid;
        # skip re-auth to avoid unnecessary OAuth round-trips.
        if (
            self.status == AccountStatus.ACTIVE
            and time.monotonic() - self._last_auth_time < self._AUTH_TTL
        ):
            return

        async with self._auth_lock:
            # Double-checked locking: another coroutine may have
            # refreshed the token while we were waiting for the lock.
            if (
                self.status == AccountStatus.ACTIVE
                and time.monotonic() - self._last_auth_time < self._AUTH_TTL
            ):
                return

            try:
                await asyncio.to_thread(
                    self.api.auth, refresh_token=self.token_info.token
                )
                self.status = AccountStatus.ACTIVE
                self._last_auth_time = time.monotonic()
                logger.info(f"{self} 认证成功")
            except PixivError as e:
                if "auth() failed" in str(e).lower():
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(str(self)) from e
                raise PixivApiError(str(e)) from e

    async def execute(self, method: str, *args, **kwargs):
        """Call an API method on this account, handling auth and rate limits."""
        await self.authenticate()

        # Honor any active cooldown before touching the request lock, so a
        # cooling account returned by select() (including the force_account
        # path) is made to actually wait before issuing its next request.
        if self.in_cooldown:
            wait = self.cooldown_remaining
            logger.warning(f"{self} 处于冷却中，等待 {wait:.0f} 秒")
            await asyncio.sleep(wait)

        # Per-account rate limiting: ensure min_interval since the
        # *last completed* API call on this account (not since select).
        async with self._req_lock:
            elapsed = time.monotonic() - self._last_call_end
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)

            try:
                func = getattr(self.api, method)
                logger.info(f"{self} API → {method}({_fmt_args(args, kwargs)})")
                result = await asyncio.to_thread(func, *args, **kwargs)
                self._last_call_end = time.monotonic()

                # -- Safety net: detect auth errors returned in the response
                # body rather than raised as exceptions.  The pixivpy3
                # monkey-patches in ``patch.py`` may return an error dict
                # (``{"error": {...}}``) instead of letting pixivpy3 raise.
                if isinstance(result, dict) and "error" in result:
                    error_data = result["error"]
                    error_msg = (
                        error_data.get("message", "")
                        if isinstance(error_data, dict)
                        else str(error_data)
                    )
                    if any(
                        kw in error_msg.lower()
                        for kw in ("oauth", "invalid_grant", "access token")
                    ):
                        logger.warning(
                            f"{self} API returned auth error in body, "
                            f"re-authenticating: {error_msg}",
                        )
                        # Force re-auth and retry once with a fresh token.
                        self.status = AccountStatus.INACTIVE
                        await self.authenticate()
                        result = await asyncio.to_thread(func, *args, **kwargs)
                        self._last_call_end = time.monotonic()

                logger.debug(f"{self} {method} completed.")
                return result
            except PixivError as e:
                error_msg = str(e).lower()
                if "invalid_grant" in error_msg:
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(str(self)) from e
                if "rate limit" in error_msg or "currently restricted" in error_msg:
                    self.start_cooldown()
                    raise RateLimitError(str(self)) from e
                raise PixivApiError(str(e)) from e
