"""Async HTTP transport client for ESJZone with pacing, auth, and unlock flow."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TypeVar
from urllib.parse import urljoin, urlparse
from weakref import WeakKeyDictionary

import httpx

from copixiv.core.exceptions import DomainError, NotFoundError, ValidationError
from copixiv.core.models import SOURCE_ESJ
from copixiv.core.sources import SourceCredentialRecord, SourceRuntimeConfig, SourceTestResult
from copixiv.sources.esj.parser import (
    DEFAULT_ESJ_BASE_URL,
    extract_jinjing_token,
    is_challenge_html,
    is_password_challenge_html,
)
from copixiv.sources.http import SourceHttpManager

_T = TypeVar("_T")

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_PROFILE_NAME_RE = re.compile(r"""href=["'][^"']*/my/profile\?uid=(\d+)[^"']*["'][^>]*>([^<]+)<""")


class EsjError(DomainError):
    """Base domain/transport error raised by the ESJZone client."""


class EsjChallengeError(EsjError):
    """Raised when Cloudflare Turnstile, WAF, or HTTP 403/503 blocks access."""


class EsjPasswordRequiredError(EsjError):
    """Raised when a chapter requires a password or password unlock fails."""


class EsjNotFoundError(EsjError, NotFoundError):
    """Raised when an ESJZone book or chapter does not exist (HTTP 404)."""


class EsjRequestError(EsjError):
    """Raised on non-404/403/503 HTTP errors or network failures."""

    def __init__(self, detail: str = "", *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(detail)


def normalize_esj_base_url(raw: str | None) -> str:
    """Validate and normalize an ESJZone base URL (defaulting blank input)."""
    if raw is None:
        return DEFAULT_ESJ_BASE_URL
    cleaned = raw.strip()
    if not cleaned:
        return DEFAULT_ESJ_BASE_URL
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("ESJ 站点地址必须是有效的 http/https URL")
    path = parsed.path.rstrip("/")
    return f"{scheme}://{parsed.netloc}{path}"


def resolve_esj_base_url(runtime: SourceRuntimeConfig) -> str:
    """Resolve the effective normalized ESJZone base URL from *runtime*."""
    raw = runtime.values.get("base_url")
    return normalize_esj_base_url(raw if isinstance(raw, str) else None)


def resolve_chapter_password(
    book_id: str,
    chapter_id: str,
    runtime: SourceRuntimeConfig,
    explicit_password: str | None = None,
) -> str | None:
    """Resolve chapter password: explicit arg > ``book/chapter`` > ``book`` > ``*``."""
    if explicit_password is not None and explicit_password.strip():
        return explicit_password
    raw_map = runtime.values.get("chapter_passwords")
    if not isinstance(raw_map, Mapping):
        return None
    for candidate in (f"{book_id}/{chapter_id}", book_id, "*"):
        val = raw_map.get(candidate)
        if isinstance(val, str) and val != "":
            return val
    return None


def _select_credential(credentials: Sequence[SourceCredentialRecord]) -> SourceCredentialRecord | None:
    ordered = sorted((c for c in credentials if c.valid), key=lambda c: (c.sort_index, c.id))
    for cred in ordered:
        if cred.kind == "cookie":
            cookie_val = cred.secret.get("cookie")
            if isinstance(cookie_val, str) and cookie_val.strip():
                return cred
        elif cred.kind == "password":
            user_val = cred.secret.get("username")
            pass_val = cred.secret.get("password")
            if (
                isinstance(user_val, str)
                and bool(user_val.strip())
                and isinstance(pass_val, str)
                and bool(pass_val)
            ):
                return cred
    return None


def _parse_cookie_pairs(cookie_str: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for segment in cookie_str.split(";"):
        part = segment.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        clean_name = name.strip()
        if clean_name:
            pairs[clean_name] = value.strip()
    return pairs


def _build_request_headers(
    base_url: str,
    client: httpx.AsyncClient,
    cookie_str: str | None,
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Referer": f"{base_url}/",
        "Accept-Language": "zh-TW,zh-CN;q=0.9,zh;q=0.8,en;q=0.7",
    }
    if cookie_str:
        parsed_cred = _parse_cookie_pairs(cookie_str)
        client_cookies = dict(client.cookies)
        merged = {**parsed_cred, **client_cookies}
        if merged:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())
        else:
            headers["Cookie"] = cookie_str.strip()
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _check_response(resp: httpx.Response, *, url: str) -> str:
    if resp.status_code in (403, 503):
        raise EsjChallengeError(
            f"ESJZone 返回 HTTP {resp.status_code}（可能触发 Cloudflare/WAF 验证或节点被屏蔽），"
            f"请检查代理或 Cookie：{url}"
        )
    if resp.status_code == 404:
        raise EsjNotFoundError(f"ESJZone 页面不存在（HTTP 404）：{url}")
    text = resp.text
    if is_challenge_html(text):
        raise EsjChallengeError(
            f"ESJZone 返回 Cloudflare Turnstile/人机验证页面，请检查代理或更新 Cookie：{url}"
        )
    if resp.status_code >= 400:
        raise EsjRequestError(
            f"ESJZone 请求失败（HTTP {resp.status_code}）：{url}",
            status_code=resp.status_code,
        )
    return text


def _is_logged_in_profile_html(html: str) -> bool:
    lowered = html.lower()
    has_logout = "/my/logout" in lowered or "登出" in html
    has_login_cue = "inc/mem_login" in lowered or 'name="pwd"' in lowered or "/my/login" in lowered
    return not (has_login_cue and not has_logout)


def _extract_profile_summary(html: str, fallback: str) -> str:
    match = _PROFILE_NAME_RE.search(html)
    if match:
        uid, name = match.group(1), match.group(2).strip()
        if name:
            return f"{name} (uid:{uid})"
        return f"uid:{uid}"
    return fallback


class EsjClient:
    """Rate-limited async HTTP client for ESJZone HTML fetching and auth."""

    def __init__(
        self,
        http_manager: SourceHttpManager | None = None,
        *,
        default_request_interval: float = 1.0,
    ) -> None:
        self._http_manager = http_manager if http_manager is not None else SourceHttpManager()
        self._owns_http_manager = http_manager is None
        self._default_request_interval = max(0.0, float(default_request_interval))
        self._last_request_end: float = 0.0
        self._locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
        self._logged_in_sessions: set[tuple[int, str, int, str]] = set()

    @property
    def http_manager(self) -> SourceHttpManager:
        """Return the underlying :class:`SourceHttpManager`."""
        return self._http_manager

    def reconfigure(self, *, request_interval_sec: float | None = None) -> None:
        """Update default request pacing interval."""
        if request_interval_sec is not None:
            self._default_request_interval = max(0.0, float(request_interval_sec))

    def reset_session(self) -> None:
        """Clear cached password-login session markers (e.g. after credential reload)."""
        self._logged_in_sessions.clear()

    async def aclose(self) -> None:
        """Close the internal ``SourceHttpManager`` if owned by this client."""
        if self._owns_http_manager:
            await self._http_manager.aclose()

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

    def _resolve_interval(self, runtime: SourceRuntimeConfig) -> float:
        raw = runtime.values.get("request_interval_sec", self._default_request_interval)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return self._default_request_interval

    async def _throttled(
        self,
        runtime: SourceRuntimeConfig,
        call: Callable[[], Awaitable[_T]],
    ) -> _T:
        async with self._get_lock():
            interval = self._resolve_interval(runtime)
            if interval > 0 and self._last_request_end > 0:
                elapsed = time.monotonic() - self._last_request_end
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)
            try:
                return await call()
            finally:
                self._last_request_end = time.monotonic()

    async def _send(
        self,
        client: httpx.AsyncClient,
        runtime: SourceRuntimeConfig,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        async def _do_request() -> httpx.Response:
            try:
                return await client.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    follow_redirects=follow_redirects,
                )
            except httpx.HTTPError as exc:
                raise EsjRequestError(f"ESJZone 网络请求异常：{exc}") from exc

        return await self._throttled(runtime, _do_request)

    async def _ensure_auth(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> tuple[SourceCredentialRecord | None, str | None]:
        cred = _select_credential(credentials)
        if cred is None:
            return None, None
        if cred.kind == "cookie":
            return cred, str(cred.secret["cookie"]).strip()

        username = str(cred.secret["username"])
        password = str(cred.secret["password"])
        session_key = (id(client), base_url, cred.id, username)
        if session_key in self._logged_in_sessions:
            return cred, None

        login_url = f"{base_url}/inc/mem_login.php"
        headers = _build_request_headers(base_url, client, None)
        resp = await self._send(
            client,
            runtime,
            "POST",
            login_url,
            headers=headers,
            data={"email": username, "pwd": password, "remember_me": "on"},
        )
        text = _check_response(resp, url=login_url)
        if text.lstrip().startswith("{"):
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                status_val = payload.get("status")
                if status_val is not None and str(status_val) != "200":
                    msg = str(payload.get("msg") or "账号或密码错误")
                    raise EsjRequestError(f"ESJZone 登录失败：{msg}")
                if payload.get("success") is False or payload.get("error"):
                    msg = str(payload.get("msg") or payload.get("error") or "登录失败")
                    raise EsjRequestError(f"ESJZone 登录失败：{msg}")

        self._logged_in_sessions.add(session_key)
        return cred, None

    async def fetch_book_html(
        self,
        book_id: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> str:
        """Fetch raw HTML for an ESJZone book detail page."""
        client = self._http_manager.get_client(SOURCE_ESJ)
        base_url = resolve_esj_base_url(runtime)
        _, cookie_str = await self._ensure_auth(client, base_url, runtime, credentials)
        url = f"{base_url}/detail/{book_id}.html"
        headers = _build_request_headers(base_url, client, cookie_str)
        resp = await self._send(client, runtime, "GET", url, headers=headers)
        return _check_response(resp, url=url)

    async def fetch_chapter_html(
        self,
        book_id: str,
        chapter_id: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
        *,
        password: str | None = None,
        unlock_token_url: str | None = None,
    ) -> str:
        """Fetch raw HTML for an ESJZone chapter, unlocking password protection once if needed."""
        client = self._http_manager.get_client(SOURCE_ESJ)
        base_url = resolve_esj_base_url(runtime)
        _, cookie_str = await self._ensure_auth(client, base_url, runtime, credentials)
        chapter_url = f"{base_url}/forum/{book_id}/{chapter_id}.html"
        headers = _build_request_headers(base_url, client, cookie_str)

        resp = await self._send(client, runtime, "GET", chapter_url, headers=headers)
        html = _check_response(resp, url=chapter_url)
        if not is_password_challenge_html(html):
            return html

        pw = resolve_chapter_password(book_id, chapter_id, runtime, password)
        if pw is None:
            raise EsjPasswordRequiredError(
                f"章节 {book_id}/{chapter_id} 需要访问密码，请配置 chapter_passwords 或传入密码"
            )

        # Single unlock attempt guard (max_unlock_attempts=1) prevents infinite loops on HTTP 200 locks.
        token_url = (
            urljoin(f"{base_url}/", unlock_token_url)
            if unlock_token_url
            else f"{base_url}/forum/{chapter_id}/{book_id}.html"
        )
        token_headers = _build_request_headers(base_url, client, cookie_str)
        token_resp = await self._send(
            client,
            runtime,
            "POST",
            token_url,
            headers=token_headers,
            data={"plxf": "getAuthToken"},
        )
        token_html = _check_response(token_resp, url=token_url)
        token = extract_jinjing_token(token_html)
        if not token:
            raise EsjPasswordRequiredError(f"章节 {book_id}/{chapter_id} 密码解锁失败：未能提取 JinJing 令牌")

        pw_url = f"{base_url}/inc/forum_pw.php"
        pw_headers = _build_request_headers(
            base_url,
            client,
            cookie_str,
            extra_headers={"authorization": token},
        )
        pw_resp = await self._send(
            client,
            runtime,
            "POST",
            pw_url,
            headers=pw_headers,
            data={"pw": pw},
        )
        _check_response(pw_resp, url=pw_url)

        refetch_headers = _build_request_headers(base_url, client, cookie_str)
        unlocked_resp = await self._send(client, runtime, "GET", chapter_url, headers=refetch_headers)
        unlocked_html = _check_response(unlocked_resp, url=chapter_url)
        if is_password_challenge_html(unlocked_html):
            raise EsjPasswordRequiredError(f"章节 {book_id}/{chapter_id} 密码错误或解锁后仍处于锁定状态")
        return unlocked_html

    async def test_connection(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> SourceTestResult:
        """Probe ``GET {base_url}/my/profile`` to verify reachability, WAF status, and login state."""
        client = self._http_manager.get_client(SOURCE_ESJ)
        base_url = resolve_esj_base_url(runtime)
        profile_url = f"{base_url}/my/profile"
        started = time.monotonic()
        cred: SourceCredentialRecord | None = _select_credential(credentials)

        try:
            cred, cookie_str = await self._ensure_auth(client, base_url, runtime, credentials)
            headers = _build_request_headers(base_url, client, cookie_str)
            resp = await self._send(
                client,
                runtime,
                "GET",
                profile_url,
                headers=headers,
                follow_redirects=False,
            )
            latency_ms = max(0, round((time.monotonic() - started) * 1000))

            if resp.status_code in (301, 302, 303, 307, 308):
                if is_challenge_html(resp.text):
                    raise EsjChallengeError(f"ESJZone 返回 Cloudflare Turnstile/人机验证页面：{profile_url}")
                location = resp.headers.get("location", "")
                if "login" in location.lower() or location:
                    if cred is not None:
                        return SourceTestResult(
                            ok=False,
                            latency_ms=latency_ms,
                            message="ESJZone 站点可达，但当前凭据未登录或已失效（跳转至登录页）",
                            account_summary=f"{cred.name}（未登录）",
                        )
                    return SourceTestResult(
                        ok=True,
                        latency_ms=latency_ms,
                        message="ESJZone 站点可达（匿名访问模式，未配置凭据）",
                        account_summary="匿名访问",
                    )

            html = _check_response(resp, url=profile_url)
            if _is_logged_in_profile_html(html):
                fallback_name = cred.name if cred is not None else "已登录"
                return SourceTestResult(
                    ok=True,
                    latency_ms=latency_ms,
                    message="ESJZone 连接测试成功（已登录）",
                    account_summary=_extract_profile_summary(html, fallback_name),
                )

            if cred is not None:
                return SourceTestResult(
                    ok=False,
                    latency_ms=latency_ms,
                    message="ESJZone 站点可达，但当前凭据未登录或已失效",
                    account_summary=f"{cred.name}（未登录）",
                )
            return SourceTestResult(
                ok=True,
                latency_ms=latency_ms,
                message="ESJZone 站点可达（匿名访问模式，未配置凭据）",
                account_summary="匿名访问",
            )
        except EsjChallengeError as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return SourceTestResult(
                ok=False,
                latency_ms=latency_ms,
                message=exc.detail,
                account_summary=cred.name if cred is not None else None,
            )
        except (EsjError, httpx.HTTPError) as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return SourceTestResult(
                ok=False,
                latency_ms=latency_ms,
                message=f"ESJZone 连接失败：{exc}",
                account_summary=cred.name if cred is not None else None,
            )


__all__ = [
    "EsjChallengeError",
    "EsjClient",
    "EsjError",
    "EsjNotFoundError",
    "EsjPasswordRequiredError",
    "EsjRequestError",
    "normalize_esj_base_url",
    "resolve_chapter_password",
    "resolve_esj_base_url",
]
