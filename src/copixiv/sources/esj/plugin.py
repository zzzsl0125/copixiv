"""EsjSourcePlugin — ESJZone multi-chapter web novel source plugin."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_ESJ
from copixiv.core.sources import (
    CredentialFieldSpec,
    CredentialKindSpec,
    EpubPolicy,
    ExternalAuthor,
    ExternalBookOutline,
    ExternalChapter,
    ParsedSourceRef,
    SettingFieldOption,
    SettingFieldSpec,
    SourceCapability,
    SourceCredentialRecord,
    SourceManifest,
    SourceRuntimeConfig,
    SourceShelfDef,
    SourceTestResult,
)
from copixiv.sources.base import BaseSourcePlugin
from copixiv.sources.esj.client import (
    EsjClient,
    EsjPasswordRequiredError,
    normalize_esj_base_url,
    resolve_esj_base_url,
)
from copixiv.sources.esj.parser import (
    DEFAULT_ESJ_BASE_URL,
    build_esj_author_key,
    parse_book_outline,
    parse_chapter,
    parse_esj_ref,
)
from copixiv.sources.http import SourceHttpManager


class EsjSourcePlugin(BaseSourcePlugin):
    """Source plugin for ESJZone books and forum chapters."""

    def __init__(
        self,
        http_manager: SourceHttpManager | None = None,
        client: EsjClient | None = None,
        *,
        default_request_interval: float = 1.0,
        default_task_timeout: int = 7200,
    ) -> None:
        self._http_manager = http_manager
        self._default_request_interval = max(0.0, float(default_request_interval))
        self._default_task_timeout = default_task_timeout
        self._client = client or EsjClient(
            http_manager=http_manager,
            default_request_interval=self._default_request_interval,
        )

        self.manifest = SourceManifest(
            source=SOURCE_ESJ,
            display_name="ESJZone",
            badge_label="ESJ",
            badge_class="bg-indigo-100 text-indigo-800",
            enabled_by_default=False,
            capabilities=frozenset(
                {
                    SourceCapability.FETCH_BY_REF,
                    SourceCapability.SYNC_NOVEL,
                    SourceCapability.RETRY_FAILURE,
                    SourceCapability.AUTH_POOL,
                    SourceCapability.TEST_CONNECTION,
                }
            ),
            editable_fields=("series_index", "tags", "create_time"),
            epub_policy=EpubPolicy.BEST_EFFORT_EXTERNAL,
            allow_proxy_config=True,
            shelf=SourceShelfDef(
                section_id="esj",
                label="ESJ",
                keyword="source:esj;",
                min_like=0,
                min_text=0,
                order_by="id",
                order_direction="DESC",
                icon="globe",
                intake_action="ref_fetch",
                intake_placeholder="输入 ESJ 书本或章节 URL...",
                intake_button_text="抓取 ESJ",
            ),
            settings_schema=(
                SettingFieldSpec(
                    key="base_url",
                    label="站点域名地址",
                    field_type="string",
                    default=DEFAULT_ESJ_BASE_URL,
                    description="支持站点域名轮换（如 .cc / .one / .me），切换后即时生效",
                ),
                SettingFieldSpec(
                    key="proxy_mode",
                    label="代理模式",
                    field_type="select",
                    default="direct",
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
                    key="request_interval_sec",
                    label="单次请求最小间隔（秒）",
                    field_type="number",
                    default=self._default_request_interval,
                    min_value=0.0,
                    max_value=60.0,
                ),
                SettingFieldSpec(
                    key="chapter_passwords",
                    label="章节密码表",
                    field_type="key_value",
                    default={},
                    description="按 book_id/chapter_id → book_id → * 顺序匹配锁定章节密码",
                ),
                SettingFieldSpec(
                    key="download_images",
                    label="下载正文插图与封面",
                    field_type="boolean",
                    default=True,
                ),
                SettingFieldSpec(
                    key="task_timeout_sec",
                    label="单次抓取任务超时（秒）",
                    field_type="number",
                    default=self._default_task_timeout,
                    min_value=0,
                    max_value=86400,
                    description="限制单次抓取任务的最长运行时间（0 表示不限制，长篇连载建议设为 7200 或 0）",
                ),
            ),
            credential_kinds=(
                CredentialKindSpec(
                    kind="cookie",
                    label="Cookie 凭据",
                    secret_fields=(
                        CredentialFieldSpec(
                            key="cookie",
                            label="Cookie",
                            field_type="secret",
                            required=True,
                        ),
                    ),
                    meta_fields=(),
                    supports_reorder=True,
                ),
                CredentialKindSpec(
                    kind="password",
                    label="账号密码",
                    secret_fields=(
                        CredentialFieldSpec(
                            key="username",
                            label="账号",
                            field_type="string",
                            required=True,
                        ),
                        CredentialFieldSpec(
                            key="password",
                            label="密码",
                            field_type="secret",
                            required=True,
                        ),
                    ),
                    meta_fields=(),
                    supports_reorder=True,
                ),
            ),
        )

    def validate_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate ESJ settings, normalizing ``base_url`` and checking ``chapter_passwords``."""
        validated = super().validate_config(raw)
        validated["base_url"] = normalize_esj_base_url(validated.get("base_url"))
        return validated

    def parse_ref(self, raw: str, *, allow_bare_id: bool = False) -> ParsedSourceRef | None:
        """Parse an ESJZone URL, ``esj:`` ref, or bare book/chapter ID when allowed."""
        return parse_esj_ref(raw, allow_bare_id=allow_bare_id)

    def build_external_url(
        self,
        kind: Literal["novel", "series", "author"],
        source_key: str,
        config: SourceRuntimeConfig,
    ) -> str | None:
        """Build the external ESJZone URL for a chapter, book, or uid-based author."""
        if not source_key:
            return None
        base_url = resolve_esj_base_url(config)
        if kind == "novel":
            if "/" not in source_key:
                return None
            book_id, chapter_id = source_key.split("/", 1)
            book_id = book_id.strip()
            chapter_id = chapter_id.strip()
            if not book_id or not chapter_id:
                return None
            return f"{base_url}/forum/{book_id}/{chapter_id}.html"
        if kind == "series":
            cleaned = source_key.strip()
            return f"{base_url}/detail/{cleaned}.html" if cleaned else None
        if kind == "author":
            if source_key.startswith("uid:"):
                uid = source_key.removeprefix("uid:").strip()
                return f"{base_url}/my/profile?uid={uid}" if uid else None
            return None
        return None

    def on_reload(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> None:
        """Hot-reconfigure ESJ HTTP client, proxy, Referer header, pacing, and session cache."""
        del credentials
        base_url = resolve_esj_base_url(runtime)
        interval_raw = runtime.values.get("request_interval_sec", self._default_request_interval)
        try:
            interval = max(0.0, float(interval_raw))
        except (TypeError, ValueError):
            interval = self._default_request_interval

        self._client.reconfigure(request_interval_sec=interval)
        self._client.reset_session()

        manager = self._http_manager or self._client.http_manager
        if manager is not None:
            manager.configure_source(
                SOURCE_ESJ,
                proxy_mode=runtime.proxy_mode,
                proxy_url=runtime.proxy_url,
                image_headers={"Referer": f"{base_url}/"},
                strip_cookies=False,
            )
            manager.clear_cookies(SOURCE_ESJ)

    async def fetch_book_outline(
        self,
        series_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> ExternalBookOutline:
        """Fetch and parse an ESJZone book detail page into an :class:`ExternalBookOutline`."""
        base_url = resolve_esj_base_url(runtime)
        html = await self._client.fetch_book_html(series_key, runtime, credentials)
        return parse_book_outline(html, series_key, base_url)

    async def fetch_chapter(
        self,
        book_key: str,
        chapter_key: str,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
        *,
        password: str | None = None,
    ) -> ExternalChapter:
        """Fetch and parse one ESJZone chapter into an :class:`ExternalChapter`."""
        base_url = resolve_esj_base_url(runtime)
        chapter_url = f"{base_url}/forum/{book_key}/{chapter_key}.html"
        html = await self._client.fetch_chapter_html(
            book_key,
            chapter_key,
            runtime,
            credentials,
            password=password,
        )
        parsed = parse_chapter(html, book_key, chapter_key, chapter_url, base_url)
        if parsed.requires_password:
            raise EsjPasswordRequiredError(f"章节 {book_key}/{chapter_key} 仍处于密码锁定状态")

        download_images = bool(runtime.values.get("download_images", True))
        author: ExternalAuthor | None = None
        if parsed.poster_uid:
            author = ExternalAuthor(source_key=f"uid:{parsed.poster_uid}", name=parsed.poster_name)
        elif parsed.poster_name:
            author = ExternalAuthor(source_key=f"name:{parsed.poster_name}", name=parsed.poster_name)

        return ExternalChapter(
            source_key=f"{book_key}/{chapter_key}",
            title=parsed.title,
            series_index=None,
            author=author,
            create_time=parsed.create_time,
            like=0,
            view=0,
            tags=(),
            content=parsed.content,
            images=parsed.images if download_images else {},
            cover_url=None,
            is_external_link=False,
        )

    async def fetch_novel_for_sync(
        self,
        source_key: str,
        stored_path: str | None,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> ExternalChapter | None:
        """Re-fetch authoritative chapter and book-level metadata for single-novel sync."""
        del stored_path
        if "/" not in source_key:
            raise ValidationError(f"无效的 ESJ 章节 source_key：{source_key!r}")
        book_id, chapter_id = source_key.split("/", 1)
        book_id = book_id.strip()
        chapter_id = chapter_id.strip()
        if not book_id or not chapter_id:
            raise ValidationError(f"无效的 ESJ 章节 source_key：{source_key!r}")

        outline = await self.fetch_book_outline(book_id, runtime, credentials)
        chapter = await self.fetch_chapter(book_id, chapter_id, runtime, credentials)

        series_index: int | None = None
        canonical_key = f"{book_id}/{chapter_id}"
        for entry in outline.chapter_entries:
            if not entry.is_external_link and entry.source_key == canonical_key:
                series_index = entry.series_index
                break

        poster_uid: str | None = None
        if chapter.author and chapter.author.source_key.startswith("uid:"):
            poster_uid = chapter.author.source_key.removeprefix("uid:") or None

        is_original = bool(outline.extra.get("is_original", False))
        extra_author = outline.extra.get("author_name")
        detail_author = (
            extra_author.strip()
            if isinstance(extra_author, str) and extra_author.strip()
            else (outline.author.name if outline.author and outline.author.name else None)
            or (chapter.author.name if chapter.author and chapter.author.name else None)
            or "佚名"
        )
        author_key = build_esj_author_key(
            detail_author,
            uid=poster_uid,
            is_original=is_original,
        )
        author = ExternalAuthor(source_key=author_key, name=detail_author)

        return chapter.model_copy(
            update={
                "series_index": series_index,
                "author": author,
                "like": outline.like,
                "view": outline.view,
                "tags": outline.tags,
                "cover_url": outline.series.cover_url,
            }
        )

    async def test_connection(
        self,
        runtime: SourceRuntimeConfig,
        credentials: Sequence[SourceCredentialRecord],
    ) -> SourceTestResult:
        """Verify ESJZone reachability, WAF status, and credential login state."""
        return await self._client.test_connection(runtime, credentials)


__all__ = ["EsjSourcePlugin"]
