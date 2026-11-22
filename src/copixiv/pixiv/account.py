"""Single Pixiv account — native async ``httpx`` transport with OAuth and rate-limiting."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx

from copixiv.log import logger

from .errors import (
    AccountInvalidError,
    PixivApiError,
    RateLimitError,
)
from .http import (
    PIXIV_API_HEADERS,
    parse_retry_after,
)

# Re-exported for callers that historically imported them from here.
__all__ = [
    "AccountInvalidError",
    "AccountStatus",
    "AccountStrategy",
    "PixivAccount",
    "PixivApiError",
    "RateLimitError",
    "TokenInfo",
]

PIXIV_APP_HOSTS = "https://app-api.pixiv.net"
PIXIV_AUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"


@dataclass
class TokenInfo:
    token: str
    username: str
    premium: bool = False
    valid: bool = True
    # Designated「追更账号」—— the account that owns the Pixiv following
    # feed (see is_follow on the tokens table).
    follow: bool = False


@dataclass
class AccountStrategy:
    need_premium: bool = False
    # Prefer the single account flagged ``follow`` over the generic LRU selection.
    force_follow: bool = False


class AccountStatus(Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    INVALID = "invalid"


class PixivAccount:
    """Single-account Pixiv HTTP transport with OAuth, pacing, and cooldown handling."""

    def __init__(
        self,
        token_info: TokenInfo,
        http_client: httpx.AsyncClient,
        min_interval: float = 2.0,
        cooling_duration: float = 120.0,
    ):
        self.token_info = token_info
        self._http_client = http_client

        self.status = AccountStatus.INACTIVE
        self.username = token_info.username
        self._access_token: str | None = None

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

    # -- actions -------------------------------------------------------------

    def start_cooldown(self, duration: float | None = None) -> None:
        self._cooldown_until = time.monotonic() + (self.cooling_duration if duration is None else duration)

    async def authenticate(self) -> None:
        if self.status == AccountStatus.INVALID:
            raise AccountInvalidError(str(self))

        if (
            self.status == AccountStatus.ACTIVE
            and self._access_token is not None
            and time.monotonic() - self._last_auth_time < self._AUTH_TTL
        ):
            return

        async with self._auth_lock:
            if (
                self.status == AccountStatus.ACTIVE
                and self._access_token is not None
                and time.monotonic() - self._last_auth_time < self._AUTH_TTL
            ):
                return

            local_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            client_hash = hashlib.md5((local_time + PIXIV_HASH_SECRET).encode("utf-8")).hexdigest()
            headers = {
                **PIXIV_API_HEADERS,
                "x-client-time": local_time,
                "x-client-hash": client_hash,
            }
            data = {
                "get_secure_url": "1",
                "client_id": PIXIV_CLIENT_ID,
                "client_secret": PIXIV_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": self.token_info.token,
            }

            resp = await self._http_client.post(
                PIXIV_AUTH_URL,
                headers=headers,
                data=data,
                timeout=30.0,
            )

            if resp.status_code in (400, 401):
                self.status = AccountStatus.INVALID
                self._access_token = None
                raise AccountInvalidError(str(self))
            if resp.status_code == 429:
                wait = parse_retry_after(
                    resp.headers.get("retry-after"),
                    default=self.cooling_duration,
                )
                self.start_cooldown(wait)
                raise RateLimitError(str(self), retry_after=wait)
            if resp.status_code != 200:
                raise PixivApiError(
                    f"Auth HTTP {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise PixivApiError(
                    f"{self} invalid auth JSON response",
                    status_code=resp.status_code,
                ) from exc

            if isinstance(payload, dict) and "error" in payload:
                self._raise_for_error_body(payload)

            token_block = (
                payload["response"]
                if isinstance(payload, dict) and isinstance(payload.get("response"), dict)
                else payload
            )
            access_token = token_block.get("access_token") if isinstance(token_block, dict) else None
            if not access_token:
                self.status = AccountStatus.INVALID
                self._access_token = None
                raise AccountInvalidError(str(self))

            self._access_token = str(access_token)
            self.status = AccountStatus.ACTIVE
            self._last_auth_time = time.monotonic()
            logger.info(f"{self} 认证成功")

    async def _reauthenticate(self) -> None:
        self.status = AccountStatus.INACTIVE
        self._last_auth_time = 0.0
        self._access_token = None
        await self.authenticate()

    # -- error-body classification helpers ------------------------------------

    @staticmethod
    def _error_body_message(result: dict[str, Any]) -> str:
        """Extract the lower-cased ``error.message`` from an error body dict."""
        error_data = result["error"]
        return (error_data.get("message", "") if isinstance(error_data, dict) else str(error_data)).lower()

    @staticmethod
    def _is_rate_limit_message(msg: str) -> bool:
        return any(kw in msg for kw in ("rate limit", "currently restricted"))

    @staticmethod
    def _is_auth_message(msg: str) -> bool:
        return any(kw in msg for kw in ("oauth", "invalid_grant", "access token"))

    def _raise_for_error_body(self, result: dict[str, Any]) -> None:
        """Classify an ``{"error": {...}}`` response body and raise."""
        msg = self._error_body_message(result)
        if self._is_rate_limit_message(msg):
            self.start_cooldown()
            raise RateLimitError(str(self)) from None
        if self._is_auth_message(msg):
            self.status = AccountStatus.INVALID
            self._access_token = None
            raise AccountInvalidError(str(self)) from None
        raise PixivApiError(f"{self} API error in body: {msg}") from None

    # -- HTTP transport ------------------------------------------------------

    async def _send_once(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        extract_payload: Callable[[httpx.Response], dict[str, Any]] | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Issue one authenticated HTTP call and decode its payload.

        Returns ``(needs_reauth, payload_dict)``.
        """
        headers = {
            **PIXIV_API_HEADERS,
            "Authorization": f"Bearer {self._access_token}",
        }
        resp = await self._http_client.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=30.0,
        )
        self._last_call_end = time.monotonic()

        if resp.status_code == 429:
            wait = parse_retry_after(
                resp.headers.get("retry-after"),
                default=self.cooling_duration,
            )
            self.start_cooldown(wait)
            raise RateLimitError(str(self), retry_after=wait)
        if resp.status_code == 401:
            return True, {}
        if resp.status_code != 200:
            raise PixivApiError(
                f"HTTP {resp.status_code} for {method} {url}",
                status_code=resp.status_code,
            )

        if extract_payload is not None:
            payload = extract_payload(resp)
        else:
            try:
                payload = resp.json()
            except ValueError as exc:
                raise PixivApiError(
                    f"{self} invalid JSON response for {method} {url}",
                    status_code=resp.status_code,
                ) from exc

        if isinstance(payload, dict) and "error" in payload:
            if self._is_auth_message(self._error_body_message(payload)):
                return True, payload
            self._raise_for_error_body(payload)

        return False, payload

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extract_payload: Callable[[httpx.Response], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Issue an authenticated Pixiv HTTP request with cooldown, pacing, and unified re-auth."""
        if self.in_cooldown:
            wait = self.cooldown_remaining
            logger.warning(f"{self} 处于冷却中，等待 {wait:.0f} 秒")
            await asyncio.sleep(wait)

        async with self._req_lock:
            if self.in_cooldown:
                wait = self.cooldown_remaining
                logger.warning(f"{self} 处于冷却中，等待 {wait:.0f} 秒")
                await asyncio.sleep(wait)

            await self.authenticate()

            elapsed = time.monotonic() - self._last_call_end
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)

            needs_reauth, payload = await self._send_once(
                method,
                url,
                params,
                data,
                extract_payload,
            )
            if needs_reauth:
                detail = self._error_body_message(payload) if payload and "error" in payload else "HTTP 401"
                logger.warning(f"{self} API auth failed ({detail}), re-authenticating in-place")
                await self._reauthenticate()
                still_reauth, retry_payload = await self._send_once(
                    method,
                    url,
                    params,
                    data,
                    extract_payload,
                )
                if still_reauth:
                    if retry_payload and "error" in retry_payload:
                        self._raise_for_error_body(retry_payload)
                    raise PixivApiError(
                        f"HTTP 401 for {method} {url}",
                        status_code=401,
                    )
                return retry_payload

            return payload
