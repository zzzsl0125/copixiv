"""PixivSourcePlugin — adapter wrapping Pixiv App API transport and account pool."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from copixiv.core.draft import safe_get
from copixiv.core.models import SOURCE_PIXIV
from copixiv.core.sources import (
    CredentialFieldSpec,
    CredentialKindSpec,
    EpubPolicy,
    ParsedSourceRef,
    SettingFieldOption,
    SettingFieldSpec,
    SourceCapability,
    SourceCredentialRecord,
    SourceManifest,
    SourceRuntimeConfig,
    SourceShelfDef,
    SourceTestResult,
    normalize_proxy_url,
)
from copixiv.core.text import filter_chinese
from copixiv.log import logger
from copixiv.pixiv.account import (
    AccountInvalidError,
    PixivAccount,
    PixivApiError,
    RateLimitError,
    TokenInfo,
)
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.http import PIXIV_IMAGE_HEADERS
from copixiv.sources.base import BaseSourcePlugin
from copixiv.sources.http import SourceHttpManager

_PIXIV_PREFIX_RE = re.compile(r"^pixiv:(\d+)$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")
_PIXIV_HOSTS = frozenset({"pixiv.net", "www.pixiv.net"})


def _pixiv_novel_ref(novel_id: int) -> ParsedSourceRef:
    key = str(novel_id)
    return ParsedSourceRef(
        source=SOURCE_PIXIV,
        kind="chapter",
        series_key="",
        chapter_key=key,
        normalized_ref=f"https://www.pixiv.net/novel/show.php?id={key}",
    )


class PixivSourcePlugin(BaseSourcePlugin):
    """Source plugin for Pixiv novels, wrapping ``AccountPool`` and ``PixivClient``."""

    def __init__(
        self,
        http_manager: SourceHttpManager | None = None,
        account_pool: AccountPool | None = None,
        client: PixivClient | None = None,
        default_min_interval: float = 2.0,
        default_cooling_duration: float = 120.0,
        default_max_concurrency: int = 5,
    ) -> None:
        self._http_manager = http_manager
        self._account_pool = account_pool
        self._client = client
        self._default_min_interval = default_min_interval
        self._default_cooling_duration = default_cooling_duration
        self._default_max_concurrency = default_max_concurrency

        self.manifest = SourceManifest(
            source=SOURCE_PIXIV,
            display_name="Pixiv",
            badge_label="",
            badge_class="",
            capabilities=frozenset(
                {
                    SourceCapability.FETCH_BY_REF,
                    SourceCapability.SYNC_NOVEL,
                    SourceCapability.RETRY_FAILURE,
                    SourceCapability.AUTH_POOL,
                    SourceCapability.AUTHOR_FEED,
                    SourceCapability.LOOKUP_ENTITY,
                    SourceCapability.TEST_CONNECTION,
                }
            ),
            editable_fields=("series_index", "tags", "create_time"),
            epub_policy=EpubPolicy.STRICT_PLACEHOLDERS,
            allow_proxy_config=True,
            shelf=SourceShelfDef(
                section_id="source_pixiv",
                label="Pixiv",
                keyword="source:pixiv;",
                min_like=0,
                min_text=0,
                order_by="id",
                order_direction="DESC",
                icon="sparkles",
                intake_action="ref_fetch",
                intake_placeholder="粘贴 Pixiv 小说链接或作品 ID（如 12345678）",
                intake_button_text="抓取",
            ),
            settings_schema=(
                SettingFieldSpec(
                    key="proxy_mode",
                    label="代理模式",
                    field_type="select",
                    default="inherit",
                    options=(
                        SettingFieldOption(value="inherit", label="跟随全局代理"),
                        SettingFieldOption(value="direct", label="直连（不使用代理）"),
                        SettingFieldOption(value="custom", label="自定义代理"),
                    ),
                ),
                SettingFieldSpec(
                    key="proxy_url",
                    label="自定义代理地址",
                    field_type="string",
                    default="",
                    description="例如 http://127.0.0.1:7890",
                    visible_when={"proxy_mode": "custom"},
                ),
                SettingFieldSpec(
                    key="min_interval",
                    label="单账号请求最小间隔（秒）",
                    field_type="number",
                    default=self._default_min_interval,
                    min_value=0.5,
                    max_value=60.0,
                ),
                SettingFieldSpec(
                    key="cooling_duration",
                    label="限流冷却时长（秒）",
                    field_type="number",
                    default=self._default_cooling_duration,
                    min_value=10.0,
                    max_value=3600.0,
                ),
                SettingFieldSpec(
                    key="max_concurrency",
                    label="最大并发请求数",
                    field_type="number",
                    default=self._default_max_concurrency,
                    min_value=1.0,
                    max_value=20.0,
                ),
            ),
            credential_kinds=(
                CredentialKindSpec(
                    kind="refresh_token",
                    label="Refresh Token",
                    secret_fields=(
                        CredentialFieldSpec(
                            key="refresh_token",
                            label="Refresh Token",
                            field_type="secret",
                            required=True,
                        ),
                    ),
                    meta_fields=(
                        CredentialFieldSpec(
                            key="premium",
                            label="Pixiv 会员",
                            field_type="boolean",
                            default=False,
                        ),
                        CredentialFieldSpec(
                            key="is_follow",
                            label="追更账号",
                            field_type="boolean",
                            default=False,
                        ),
                    ),
                    supports_reorder=True,
                    unique_meta_flags=("is_follow",),
                ),
            ),
        )

    def parse_ref(self, raw: str, *, allow_bare_id: bool = False) -> ParsedSourceRef | None:
        """Parse a Pixiv novel URL, ``pixiv:<id>`` ref, or bare ID when allowed."""
        if not raw:
            return None
        cleaned = raw.strip()
        if not cleaned:
            return None

        prefix_match = _PIXIV_PREFIX_RE.match(cleaned)
        if prefix_match:
            novel_id = int(prefix_match.group(1))
            return _pixiv_novel_ref(novel_id) if novel_id > 0 else None

        if allow_bare_id and _DIGITS_RE.match(cleaned):
            novel_id = int(cleaned)
            return _pixiv_novel_ref(novel_id) if novel_id > 0 else None

        parsed = urlparse(cleaned)
        if (
            parsed.scheme in ("http", "https")
            and parsed.netloc.lower() in _PIXIV_HOSTS
            and parsed.path.rstrip("/") == "/novel/show.php"
        ):
            for candidate in parse_qs(parsed.query).get("id", ()):
                if _DIGITS_RE.match(candidate):
                    novel_id = int(candidate)
                    if novel_id > 0:
                        return _pixiv_novel_ref(novel_id)

        return None

    def build_external_url(
        self,
        kind: Literal["novel", "series", "author"],
        source_key: str,
        config: SourceRuntimeConfig,
    ) -> str | None:
        """Return the canonical Pixiv web URL for a novel, series, or user."""
        del config
        if not source_key:
            return None
        if kind == "novel":
            return f"https://www.pixiv.net/novel/show.php?id={source_key}"
        if kind == "series":
            return f"https://www.pixiv.net/novel/series/{source_key}"
        if kind == "author":
            return f"https://www.pixiv.net/users/{source_key}"
        return None

    def on_reload(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None:
        """Hot-reconfigure Pixiv HTTP client, pacing, concurrency, and account tokens."""
        min_interval = float(runtime.values.get("min_interval", self._default_min_interval))
        cooling_duration = float(runtime.values.get("cooling_duration", self._default_cooling_duration))
        max_concurrency = max(1, int(runtime.values.get("max_concurrency", self._default_max_concurrency)))

        http_client: httpx.AsyncClient | None = None
        if self._http_manager is not None:
            http_client = self._http_manager.configure_source(
                SOURCE_PIXIV,
                proxy_mode=runtime.proxy_mode,
                proxy_url=runtime.proxy_url,
                strip_cookies=True,
                image_headers=PIXIV_IMAGE_HEADERS,
            )

        if self._account_pool is not None:
            self._account_pool.reconfigure(
                http_client=http_client,
                min_interval=min_interval,
                cooling_duration=cooling_duration,
            )
            ordered = sorted(credentials, key=lambda c: (c.sort_index, c.id))
            token_rows: list[dict[str, Any]] = []
            for cred in ordered:
                token_val = cred.secret.get("refresh_token")
                if not isinstance(token_val, str) or not token_val.strip():
                    continue
                token_rows.append(
                    {
                        "name": cred.name,
                        "token": token_val,
                        "premium": bool(cred.meta.get("premium", False)),
                        "valid": cred.valid,
                        "is_follow": bool(cred.meta.get("is_follow", False)),
                    }
                )
            self._account_pool.reload_from_tokens(token_rows)

        if self._client is not None:
            self._client.reconfigure(max_concurrency=max_concurrency)

    async def on_author_deleted(
        self,
        author_source_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None:
        """Unfollow the Pixiv author using the designated follow account."""
        del runtime, credentials
        if self._client is None:
            return
        try:
            uid = int(author_source_key)
        except ValueError:
            return
        if uid <= 0:
            return
        async with self._client.account_rule(force_follow=True):
            await self._client.user_follow_delete(uid)

    async def fetch_author_feed(
        self,
        author_source_keys: Sequence[str],
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> list[Any]:
        """Fetch recent works for followed Pixiv authors and filter to Chinese works."""
        del runtime, credentials
        if not author_source_keys:
            return []
        if self._client is None:
            raise RuntimeError("PixivClient is not configured")

        valid_pairs: list[tuple[str, int]] = []
        for key in author_source_keys:
            try:
                uid = int(key)
            except ValueError:
                logger.error(f"Invalid Pixiv author source_key: {key!r}")
                continue
            if uid > 0:
                valid_pairs.append((key, uid))

        if not valid_pairs:
            return []

        responses = await asyncio.gather(
            *[self._client.user_novels(uid) for _, uid in valid_pairs],
            return_exceptions=True,
        )

        all_novels: list[Any] = []
        for (key, _), resp in zip(valid_pairs, responses, strict=True):
            if isinstance(resp, BaseException):
                if not isinstance(resp, Exception):
                    raise resp
                logger.error(f"Failed to fetch novels for author {key}: {resp}")
                continue
            novels = safe_get(resp, "novels", [])
            if novels:
                all_novels.extend(novels)

        return filter_chinese(all_novels)

    async def test_connection(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> SourceTestResult:
        """Test Pixiv OAuth connectivity using the first valid credential."""
        valid_creds = sorted(
            (
                c
                for c in credentials
                if c.valid
                and isinstance(c.secret.get("refresh_token"), str)
                and bool(c.secret["refresh_token"].strip())
            ),
            key=lambda c: (c.sort_index, c.id),
        )
        if not valid_creds:
            return SourceTestResult(ok=False, message="未配置有效的 Pixiv 凭据")

        first = valid_creds[0]
        owned_client: httpx.AsyncClient | None = None
        if self._http_manager is not None:
            http_client = self._http_manager.get_client(SOURCE_PIXIV)
        elif self._account_pool is not None and self._account_pool._http_client is not None:
            http_client = self._account_pool._http_client
        else:
            proxy = normalize_proxy_url(runtime.proxy_url) if runtime.proxy_mode == "custom" else None
            owned_client = httpx.AsyncClient(proxy=proxy, timeout=15.0)
            http_client = owned_client

        account = PixivAccount(
            token_info=TokenInfo(
                token=str(first.secret["refresh_token"]),
                username=first.name,
                premium=bool(first.meta.get("premium", False)),
                valid=True,
                follow=bool(first.meta.get("is_follow", False)),
            ),
            http_client=http_client,
            min_interval=0.0,
            cooling_duration=0.0,
        )

        started = time.monotonic()
        try:
            await account.authenticate()
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return SourceTestResult(
                ok=True,
                latency_ms=latency_ms,
                message="Pixiv 连接测试成功",
                account_summary=f"{first.name}（共 {len(valid_creds)} 个有效凭据）",
            )
        except AccountInvalidError:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return SourceTestResult(
                ok=False,
                latency_ms=latency_ms,
                message=f"账号 {first.name} 认证失败（Refresh Token 无效或已过期）",
                account_summary=first.name,
            )
        except RateLimitError:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return SourceTestResult(
                ok=False,
                latency_ms=latency_ms,
                message=f"账号 {first.name} 触发 Pixiv 限流",
                account_summary=first.name,
            )
        except (PixivApiError, httpx.HTTPError) as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            return SourceTestResult(
                ok=False,
                latency_ms=latency_ms,
                message=f"Pixiv 连接失败：{exc}",
                account_summary=first.name,
            )
        finally:
            if owned_client is not None:
                await owned_client.aclose()


__all__ = ["PixivSourcePlugin"]
