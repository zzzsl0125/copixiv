"""Single Pixiv account — native async ``httpx`` client with OAuth and rate-limiting."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import httpx

from copixiv.core.exceptions import NovelNotFoundError
from copixiv.log import logger

from .errors import (
    AccountInvalidError,
    PixivApiError,
    PixivHttpError,
    RateLimitError,
)
from .http import (
    PIXIV_API_HEADERS,
    normalize_proxy_url,
    parse_retry_after,
)
from .models import (
    NovelListResponse,
    UserDetailResponse,
    WebviewNovel,
)

# Re-exported for callers that historically imported them from here.
__all__ = [
    "AccountInvalidError",
    "AccountStatus",
    "AccountStrategy",
    "PixivAccount",
    "PixivApiError",
    "PixivHttpError",
    "RateLimitError",
    "TokenInfo",
]

PIXIV_APP_HOSTS = "https://app-api.pixiv.net"
PIXIV_AUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

_WEBVIEW_NOVEL_RE = re.compile(r"novel:\s({.+}),\s+isOwnWork")
_ALLOWED_NEXT_HOST = "app-api.pixiv.net"


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
    force_account: str | None = None
    # Prefer the single account flagged ``follow`` over the generic LRU /
    # force_account string selection.
    force_follow: bool = False


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


def _format_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)


class PixivAccount:
    """A single Pixiv account with OAuth state, pacing, and cooldown handling."""

    def __init__(
        self,
        token_info: TokenInfo,
        proxy_url: str = "",
        min_interval: float = 2.0,
        cooling_duration: float = 120.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.token_info = token_info
        self._proxy_url = normalize_proxy_url(proxy_url)
        self._http_client = http_client
        self._owns_client = http_client is None

        self.status = AccountStatus.INACTIVE
        self.username = token_info.username
        self._access_token: str | None = None
        self._user_id: int = 0

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

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                proxy=self._proxy_url,
                timeout=30.0,
            )
            self._owns_client = True
        return self._http_client

    async def aclose(self) -> None:
        """Close the underlying HTTP client when owned by this account."""
        if self._http_client is not None and self._owns_client:
            await self._http_client.aclose()
            self._http_client = None

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

            client = self._get_client()
            resp = await client.post(
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
                raise RateLimitError(
                    str(self),
                    retry_after=wait,
                    header=resp.headers,
                    body=resp.text,
                )
            if resp.status_code not in (200, 301, 302):
                raise PixivApiError(
                    f"Auth HTTP {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                    header=resp.headers,
                    body=resp.text,
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise PixivApiError(
                    f"{self} invalid auth JSON response",
                    status_code=resp.status_code,
                    header=resp.headers,
                    body=resp.text,
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

            user_block = token_block.get("user") if isinstance(token_block, dict) else None
            if isinstance(user_block, dict) and user_block.get("id"):
                with contextlib.suppress(TypeError, ValueError):
                    self._user_id = int(user_block["id"])

            self._access_token = str(access_token)
            self.status = AccountStatus.ACTIVE
            self._last_auth_time = time.monotonic()
            logger.info(f"{self} 认证成功")

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

    # -- request dispatch ----------------------------------------------------

    def _build_request(
        self, method: str, *args: Any, **kwargs: Any
    ) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, str, Any]:
        """Translate a high-level Pixiv method call into HTTP parameters."""
        if method == "webview_novel":
            novel_id = args[0] if args else kwargs["novel_id"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/webview/v2/novel",
                {"id": novel_id, "viewer_version": "20221031_ai"},
                None,
                "webview",
                novel_id,
            )

        if method == "user_novels":
            user_id = args[0] if args else kwargs["user_id"]
            params: dict[str, Any] = {
                "user_id": user_id,
                "filter": kwargs.get("filter", "for_ios"),
            }
            if kwargs.get("offset"):
                params["offset"] = kwargs["offset"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/v1/user/novels",
                params,
                None,
                "novel_list",
                user_id,
            )

        if method == "user_detail":
            user_id = args[0] if args else kwargs["user_id"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/v1/user/detail",
                {"user_id": user_id, "filter": kwargs.get("filter", "for_ios")},
                None,
                "user_detail",
                user_id,
            )

        if method == "user_follow_add":
            user_id = args[0] if args else kwargs["user_id"]
            return (
                "POST",
                f"{PIXIV_APP_HOSTS}/v1/user/follow/add",
                None,
                {"user_id": user_id, "restrict": kwargs.get("restrict", "public")},
                "json",
                user_id,
            )

        if method == "user_follow_delete":
            user_id = args[0] if args else kwargs["user_id"]
            return (
                "POST",
                f"{PIXIV_APP_HOSTS}/v1/user/follow/delete",
                None,
                {"user_id": user_id},
                "json",
                user_id,
            )

        if method == "novel_follow":
            params = {"restrict": kwargs.get("restrict", "public")}
            if kwargs.get("offset") is not None:
                params["offset"] = kwargs["offset"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/v1/novel/follow",
                params,
                None,
                "novel_list",
                None,
            )

        if method == "novel_ranking":
            params = {
                "mode": kwargs.get("mode", "day_r18"),
                "filter": kwargs.get("filter", "for_ios"),
            }
            if kwargs.get("date"):
                params["date"] = _format_date(kwargs["date"])
            if kwargs.get("offset"):
                params["offset"] = kwargs["offset"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/v1/novel/ranking",
                params,
                None,
                "novel_list",
                None,
            )

        if method == "search_novel":
            word = args[0] if args else kwargs["word"]
            params = {
                "word": word,
                "search_target": kwargs.get("search_target", "partial_match_for_tags"),
                "sort": kwargs.get("sort", "date_desc"),
                "merge_plain_keyword_results": kwargs.get("merge_plain_keyword_results", "true"),
                "include_translated_tag_results": kwargs.get("include_translated_tag_results", "true"),
            }
            if kwargs.get("filter") is not None:
                params["filter"] = kwargs["filter"]
            if kwargs.get("start_date"):
                params["start_date"] = _format_date(kwargs["start_date"])
            if kwargs.get("end_date"):
                params["end_date"] = _format_date(kwargs["end_date"])
            if kwargs.get("search_ai_type") is not None:
                params["search_ai_type"] = kwargs["search_ai_type"]
            if kwargs.get("offset"):
                params["offset"] = kwargs["offset"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/v1/search/novel",
                params,
                None,
                "novel_list",
                word,
            )

        if method == "novel_series":
            series_id = args[0] if args else kwargs["series_id"]
            params = {
                "series_id": series_id,
                "filter": kwargs.get("filter", "for_ios"),
            }
            if kwargs.get("last_order"):
                params["last_order"] = kwargs["last_order"]
            return (
                "GET",
                f"{PIXIV_APP_HOSTS}/v2/novel/series",
                params,
                None,
                "novel_list",
                series_id,
            )

        if method == "get_next_page":
            next_url = str(args[0] if args else kwargs["url"])
            parsed = urlparse(next_url)
            if parsed.scheme != "https" or parsed.netloc != _ALLOWED_NEXT_HOST:
                raise PixivApiError(
                    f"Refusing unexpected next_url origin: {next_url}",
                    status_code=400,
                )
            return ("GET", next_url, None, None, "novel_list", next_url)

        raise AttributeError(f"Unsupported Pixiv API method: {method}")

    async def _send_once(
        self,
        http_method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        kind: str,
        target_id: Any,
    ) -> tuple[bool, Any]:
        """Issue one authenticated HTTP call and decode/validate its response.

        Returns ``(needs_reauth, parsed_payload_or_model)``.
        """
        client = self._get_client()
        headers = {
            **PIXIV_API_HEADERS,
            "Authorization": f"Bearer {self._access_token}",
        }
        resp = await client.request(
            http_method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=30.0,
        )
        self._last_call_end = time.monotonic()

        if kind == "webview" and resp.status_code == 404:
            raise NovelNotFoundError(f"novel #{target_id} not found (HTTP 404)")
        if resp.status_code == 429:
            wait = parse_retry_after(
                resp.headers.get("retry-after"),
                default=self.cooling_duration,
            )
            self.start_cooldown(wait)
            raise RateLimitError(
                str(self),
                retry_after=wait,
                header=resp.headers,
                body=resp.text,
            )
        if resp.status_code == 401:
            self.status = AccountStatus.INACTIVE
            self._last_auth_time = 0.0
            self._access_token = None
            raise PixivApiError(
                f"HTTP 401 for {http_method} {url}",
                status_code=401,
                header=resp.headers,
                body=resp.text,
            )
        if resp.status_code not in (200, 301, 302):
            raise PixivApiError(
                f"HTTP {resp.status_code} for {http_method} {url}",
                status_code=resp.status_code,
                header=resp.headers,
                body=resp.text,
            )

        if kind == "webview":
            match = _WEBVIEW_NOVEL_RE.search(resp.text)
            if not match:
                logger.error(f"Failed to fetch novel#{target_id}: extract novel content failed")
                raise NovelNotFoundError(f"novel #{target_id} content extraction failed")
            try:
                payload = json.loads(match.group(1))
            except ValueError as exc:
                logger.error(f"Failed to parse novel#{target_id} JSON: {exc}")
                raise NovelNotFoundError(f"novel #{target_id} content extraction failed") from exc
            if isinstance(payload, dict) and "error" in payload:
                if self._is_auth_message(self._error_body_message(payload)):
                    return True, payload
                self._raise_for_error_body(payload)
            return False, WebviewNovel.model_validate(payload)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise PixivApiError(
                f"{self} invalid JSON response for {http_method} {url}",
                status_code=resp.status_code,
                header=resp.headers,
                body=resp.text,
            ) from exc

        if isinstance(payload, dict) and "error" in payload:
            if self._is_auth_message(self._error_body_message(payload)):
                return True, payload
            self._raise_for_error_body(payload)

        if kind == "novel_list":
            return False, NovelListResponse.model_validate(payload)
        if kind == "user_detail":
            return False, UserDetailResponse.model_validate(payload)
        return False, payload

    async def execute(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call a Pixiv API method on this account with auth and rate limits."""
        await self.authenticate()

        if self.in_cooldown:
            wait = self.cooldown_remaining
            logger.warning(f"{self} 处于冷却中，等待 {wait:.0f} 秒")
            await asyncio.sleep(wait)

        async with self._req_lock:
            if self.in_cooldown:
                wait = self.cooldown_remaining
                logger.warning(f"{self} 处于冷却中，等待 {wait:.0f} 秒")
                await asyncio.sleep(wait)

            elapsed = time.monotonic() - self._last_call_end
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)

            http_method, url, params, data, kind, target_id = self._build_request(method, *args, **kwargs)
            logger.info(f"{self} API → {method}({_fmt_args(args, kwargs)})")

            needs_reauth, result = await self._send_once(http_method, url, params, data, kind, target_id)
            if needs_reauth:
                logger.warning(
                    f"{self} API returned auth error in body, "
                    f"re-authenticating: {self._error_body_message(result)}",
                )
                self.status = AccountStatus.INACTIVE
                self._last_auth_time = 0.0
                self._access_token = None
                await self.authenticate()
                still_reauth, retry_result = await self._send_once(
                    http_method, url, params, data, kind, target_id
                )
                if still_reauth:
                    self._raise_for_error_body(retry_result)
                logger.debug(f"{self} {method} completed (after re-auth).")
                return retry_result

            logger.debug(f"{self} {method} completed.")
            return result
