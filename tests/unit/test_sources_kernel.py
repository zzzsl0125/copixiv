"""Layer 1 unit tests: multi-source contracts, registry, HTTP manager, Pixiv and Local plugins."""

from __future__ import annotations

from typing import Any, Literal, cast

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.factories import make_simple_epub

from copixiv.core.exceptions import ValidationError as DomainValidationError
from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV, UNKNOWN_AUTHOR
from copixiv.core.sources import (
    FALLBACK_EDITABLE_FIELDS,
    EpubPolicy,
    FallbackSourcePolicy,
    ParsedSourceRef,
    ProxyMode,
    SettingFieldOption,
    SettingFieldSpec,
    SourceCapability,
    SourceCredentialRecord,
    SourceManifest,
    SourceRuntimeConfig,
    make_fallback_manifest,
)
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.http import PIXIV_IMAGE_HEADERS
from copixiv.sources.base import BaseSourcePlugin
from copixiv.sources.http import SourceHttpManager
from copixiv.sources.local import (
    UNNAMED_TITLE,
    LocalSourcePlugin,
    ParsedUpload,
    UploadUnreadable,
    normalize_text,
    parse_filename,
    parse_upload,
    resolve_metadata,
)
from copixiv.sources.pixiv import PixivSourcePlugin
from copixiv.sources.registry import SourceRegistry
from copixiv.storage.encoding import DECODE_ERROR


class _SimpleBareIdPlugin(BaseSourcePlugin):
    """Test plugin whose ``parse_ref`` omits ``allow_bare_id`` to test registry guards."""

    manifest = SourceManifest(
        source="simple",
        display_name="Simple",
        capabilities=frozenset({SourceCapability.FETCH_BY_REF}),
        editable_fields=("title",),
        epub_policy=EpubPolicy.BEST_EFFORT_EXTERNAL,
    )

    def parse_ref(self, raw: str) -> ParsedSourceRef | None:
        if raw.isdigit():
            return ParsedSourceRef(
                source="simple",
                kind="book",
                series_key=raw,
                normalized_ref=f"simple:{raw}",
            )
        if raw.startswith("simple:"):
            key = raw.removeprefix("simple:")
            if key:
                return ParsedSourceRef(
                    source="simple",
                    kind="book",
                    series_key=key,
                    normalized_ref=f"simple:{key}",
                )
        return None

    def build_external_url(
        self,
        kind: Literal["novel", "series", "author"],
        source_key: str,
        config: SourceRuntimeConfig,
    ) -> str | None:
        del config
        return f"https://simple.example/{kind}/{source_key}"


def _make_credential(
    cred_id: int,
    *,
    name: str,
    refresh_token: str | None = "tok",
    valid: bool = True,
    sort_index: int = 0,
    premium: bool = False,
    is_follow: bool = False,
) -> SourceCredentialRecord:
    secret: dict[str, Any] = {} if refresh_token is None else {"refresh_token": refresh_token}
    return SourceCredentialRecord(
        id=cred_id,
        source=SOURCE_PIXIV,
        name=name,
        kind="refresh_token",
        secret=secret,
        valid=valid,
        sort_index=sort_index,
        meta={"premium": premium, "is_follow": is_follow},
    )


# ---------------------------------------------------------------------------
# 1. SourceManifest
# ---------------------------------------------------------------------------


def test_source_manifest_capabilities_default_config_and_immutability():
    manifest = SourceManifest(
        source="demo",
        display_name="Demo Source",
        capabilities=frozenset({SourceCapability.FETCH_BY_REF, SourceCapability.TEST_CONNECTION}),
        settings_schema=(
            SettingFieldSpec(
                key="mode",
                label="Mode",
                field_type="select",
                default="fast",
                options=(SettingFieldOption(value="fast", label="Fast"),),
            ),
            SettingFieldSpec(
                key="timeout",
                label="Timeout",
                field_type="number",
                default=15,
            ),
            SettingFieldSpec(
                key="headers",
                label="Headers",
                field_type="key_value",
                default={"X-Demo": "1"},
            ),
        ),
    )

    assert manifest.has_capability(SourceCapability.FETCH_BY_REF) is True
    assert manifest.has_capability(SourceCapability.TEST_CONNECTION) is True
    assert manifest.has_capability(SourceCapability.FILE_UPLOAD) is False
    assert manifest.has_capability(SourceCapability.AUTH_POOL) is False

    defaults = manifest.default_config()
    assert defaults == {"mode": "fast", "timeout": 15, "headers": {"X-Demo": "1"}}

    # default_config() deep-copies defaults so mutating returned dicts does not leak.
    defaults["headers"]["X-Demo"] = "changed"
    assert manifest.default_config()["headers"] == {"X-Demo": "1"}

    empty_manifest = SourceManifest(source="empty", display_name="Empty")
    assert empty_manifest.default_config() == {}

    with pytest.raises(PydanticValidationError):
        manifest.display_name = "Mutated"
    with pytest.raises(PydanticValidationError):
        manifest.settings_schema[0].label = "Mutated"


# ---------------------------------------------------------------------------
# 2. SourceRuntimeConfig.from_values()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_mode", "expected_mode"),
    [
        ("inherit", "inherit"),
        ("direct", "direct"),
        ("custom", "custom"),
        (None, "inherit"),
        ("", "inherit"),
        ("socks5", "inherit"),
        ("CUSTOM", "inherit"),
        (123, "inherit"),
    ],
)
def test_source_runtime_config_from_values_proxy_mode_coercion(
    raw_mode: Any,
    expected_mode: ProxyMode,
):
    cfg = SourceRuntimeConfig.from_values("pixiv", True, {"proxy_mode": raw_mode})
    assert cfg.source == "pixiv"
    assert cfg.enabled is True
    assert cfg.proxy_mode == expected_mode


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (None, None),
        ("", None),
        ("   \t\n  ", None),
        (1234, None),
        ("  127.0.0.1:20172  ", "http://127.0.0.1:20172"),
        ("  http://127.0.0.1:7890 ", "http://127.0.0.1:7890"),
        (" socks5://127.0.0.1:1080 ", "socks5://127.0.0.1:1080"),
    ],
)
def test_source_runtime_config_from_values_proxy_url_normalization(
    raw_url: Any,
    expected_url: str | None,
):
    raw_values: dict[str, Any] = {"proxy_mode": "custom", "proxy_url": raw_url, "extra": 42}
    cfg = SourceRuntimeConfig.from_values("pixiv", False, raw_values)

    assert cfg.enabled is False
    assert cfg.proxy_mode == "custom"
    assert cfg.proxy_url == expected_url
    assert cfg.values["extra"] == 42

    raw_values["extra"] = 99
    assert cfg.values["extra"] == 42

    with pytest.raises(PydanticValidationError):
        cfg.proxy_mode = "direct"


# ---------------------------------------------------------------------------
# 3. FallbackSourcePolicy & make_fallback_manifest()
# ---------------------------------------------------------------------------


def test_fallback_source_policy_and_make_fallback_manifest():
    manifest = make_fallback_manifest("removed_src")

    assert manifest.source == "removed_src"
    assert manifest.display_name == "removed_src"
    assert manifest.badge_label == "REMOVED_SRC"
    assert manifest.badge_class == "bg-gray-100 text-gray-700"
    assert manifest.capabilities == frozenset()
    assert all(not manifest.has_capability(cap) for cap in SourceCapability)
    assert manifest.editable_fields == FALLBACK_EDITABLE_FIELDS
    assert manifest.epub_policy is EpubPolicy.CONTAINER_ONLY
    assert manifest.shelf is None
    assert manifest.allow_proxy_config is False

    with pytest.raises(PydanticValidationError):
        manifest.allow_proxy_config = True

    assert FallbackSourcePolicy.manifest_for("removed_src") == manifest
    assert FallbackSourcePolicy.editable_fields == FALLBACK_EDITABLE_FIELDS
    assert FallbackSourcePolicy.epub_policy is EpubPolicy.CONTAINER_ONLY
    assert FallbackSourcePolicy.can_sync is False
    assert (
        FallbackSourcePolicy.external_url(
            "novel",
            "123",
            SourceRuntimeConfig(source="removed_src"),
        )
        is None
    )
    assert FallbackSourcePolicy.external_url(kind="author", source_key="1") is None


# ---------------------------------------------------------------------------
# 4. SourceRegistry
# ---------------------------------------------------------------------------


def test_source_registry_registration_ordering_and_lookups():
    empty_manifest_plugin = BaseSourcePlugin()
    empty_manifest_plugin.manifest = SourceManifest(source="", display_name="Broken")
    registry = SourceRegistry()
    with pytest.raises(ValueError, match="must not be empty"):
        registry.register(empty_manifest_plugin)

    pixiv = PixivSourcePlugin()
    local = LocalSourcePlugin()
    simple = _SimpleBareIdPlugin()
    registry = SourceRegistry([pixiv, local])
    registry.register(simple)

    assert registry.sources() == [SOURCE_PIXIV, SOURCE_LOCAL, "simple"]
    assert registry.all_plugins() == [pixiv, local, simple]
    assert registry.get(SOURCE_PIXIV) is pixiv
    assert registry.get(SOURCE_LOCAL) is local
    assert registry.get("missing") is None

    assert registry.get_manifest_or_fallback(SOURCE_PIXIV) == pixiv.manifest
    assert registry.get_manifest_or_fallback("removed_src") == make_fallback_manifest("removed_src")

    assert registry.editable_fields(SOURCE_LOCAL) == local.manifest.editable_fields
    assert registry.editable_fields("simple") == ("title",)
    assert registry.editable_fields("removed_src") == FALLBACK_EDITABLE_FIELDS

    assert registry.epub_policy(SOURCE_PIXIV) is EpubPolicy.STRICT_PLACEHOLDERS
    assert registry.epub_policy(SOURCE_LOCAL) is EpubPolicy.CONTAINER_ONLY
    assert registry.epub_policy("simple") is EpubPolicy.BEST_EFFORT_EXTERNAL
    assert registry.epub_policy("removed_src") is EpubPolicy.CONTAINER_ONLY

    assert registry.has_capability(SOURCE_PIXIV, SourceCapability.SYNC_NOVEL) is True
    assert registry.has_capability(SOURCE_LOCAL, SourceCapability.SYNC_NOVEL) is False
    assert registry.has_capability("removed_src", SourceCapability.FETCH_BY_REF) is False

    cfg = SourceRuntimeConfig(source=SOURCE_PIXIV)
    assert registry.build_external_url(SOURCE_PIXIV, "novel", "123", cfg) == (
        "https://www.pixiv.net/novel/show.php?id=123"
    )
    assert registry.build_external_url(SOURCE_LOCAL, "novel", "123", cfg) is None
    assert registry.build_external_url("removed_src", "novel", "123", cfg) is None


def test_source_registry_parse_ref_and_preferred_source():
    pixiv = PixivSourcePlugin()
    local = LocalSourcePlugin()
    simple = _SimpleBareIdPlugin()
    registry = SourceRegistry([local, pixiv, simple])

    assert registry.parse_ref("") is None
    assert registry.parse_ref("   ") is None

    # Full URL and prefixed refs resolve without preferred_source.
    url_ref = registry.parse_ref("  https://www.pixiv.net/novel/show.php?id=777  ")
    assert url_ref == ParsedSourceRef(
        source=SOURCE_PIXIV,
        kind="chapter",
        series_key="",
        chapter_key="777",
        normalized_ref="https://www.pixiv.net/novel/show.php?id=777",
    )
    assert registry.parse_ref("simple:abc") == ParsedSourceRef(
        source="simple",
        kind="book",
        series_key="abc",
        normalized_ref="simple:abc",
    )

    # Bare digits require preferred_source to disambiguate, even if a plugin omits allow_bare_id.
    assert registry.parse_ref("12345") is None
    assert registry.parse_ref("12345", preferred_source=SOURCE_LOCAL) is None
    assert registry.parse_ref("12345", preferred_source="unknown") is None

    assert registry.parse_ref("12345", preferred_source=SOURCE_PIXIV) == ParsedSourceRef(
        source=SOURCE_PIXIV,
        kind="chapter",
        series_key="",
        chapter_key="12345",
        normalized_ref="https://www.pixiv.net/novel/show.php?id=12345",
    )
    assert registry.parse_ref("12345", preferred_source="simple") == ParsedSourceRef(
        source="simple",
        kind="book",
        series_key="12345",
        normalized_ref="simple:12345",
    )

    # Mismatched preferred_source falls through to other plugins with allow_bare_id=False.
    assert registry.parse_ref(
        "https://www.pixiv.net/novel/show.php?id=42",
        preferred_source=SOURCE_LOCAL,
    ) == ParsedSourceRef(
        source=SOURCE_PIXIV,
        kind="chapter",
        series_key="",
        chapter_key="42",
        normalized_ref="https://www.pixiv.net/novel/show.php?id=42",
    )


# ---------------------------------------------------------------------------
# 5. SourceHttpManager
# ---------------------------------------------------------------------------


def test_source_http_manager_resolve_proxy():
    manager = SourceHttpManager(global_proxy_url=" 127.0.0.1:7890 ")

    assert manager.resolve_proxy("inherit", "http://ignored:1234") == "http://127.0.0.1:7890"
    assert manager.resolve_proxy("direct", "http://ignored:1234") is None
    assert manager.resolve_proxy("custom", " 127.0.0.1:20172 ") == "http://127.0.0.1:20172"
    assert manager.resolve_proxy("custom", " socks5://127.0.0.1:1080 ") == "socks5://127.0.0.1:1080"
    assert manager.resolve_proxy("custom", "") is None
    assert manager.resolve_proxy("custom", "   ") is None
    assert manager.resolve_proxy("custom", None) is None

    blank_global = SourceHttpManager(global_proxy_url="   ")
    assert blank_global.resolve_proxy("inherit", None) is None


@pytest.mark.asyncio
async def test_source_http_manager_configure_source_reuse_and_retirement():
    manager = SourceHttpManager(global_proxy_url="127.0.0.1:7890", timeout=30.0)
    try:
        c1 = manager.configure_source(
            "pixiv",
            proxy_mode="inherit",
            proxy_url="ignored:9999",
            strip_cookies=True,
            image_headers={"Referer": "https://www.pixiv.net/", "User-Agent": "PixivTest"},
            timeout=20.0,
        )
        # Same effective spec (including different header insertion order) reuses the client.
        c2 = manager.configure_source(
            "pixiv",
            proxy_mode="inherit",
            proxy_url="other_ignored:8888",
            strip_cookies=True,
            image_headers={"User-Agent": "PixivTest", "Referer": "https://www.pixiv.net/"},
            timeout=20.0,
        )
        assert c2 is c1
        assert manager._retired == []
        assert manager.get_client("pixiv") is c1
        downloader = manager.get_image_downloader("pixiv")
        assert downloader._http_client is c1
        assert downloader._default_headers == {
            "Referer": "https://www.pixiv.net/",
            "User-Agent": "PixivTest",
        }

        # Changing proxy_mode retires old client and swaps in a new one.
        c3 = manager.configure_source(
            "pixiv",
            proxy_mode="direct",
            strip_cookies=True,
            image_headers={"Referer": "https://www.pixiv.net/", "User-Agent": "PixivTest"},
            timeout=20.0,
        )
        assert c3 is not c1
        assert manager._retired == [c1]

        # Changing proxy_url (when proxy_mode="custom") swaps.
        c4 = manager.configure_source(
            "pixiv",
            proxy_mode="custom",
            proxy_url="127.0.0.1:8080",
            strip_cookies=True,
            image_headers={"Referer": "https://www.pixiv.net/", "User-Agent": "PixivTest"},
            timeout=20.0,
        )
        c5 = manager.configure_source(
            "pixiv",
            proxy_mode="custom",
            proxy_url="127.0.0.1:9090",
            strip_cookies=True,
            image_headers={"Referer": "https://www.pixiv.net/", "User-Agent": "PixivTest"},
            timeout=20.0,
        )
        assert c4 is not c3
        assert c5 is not c4
        assert manager._retired == [c1, c3, c4]

        # Changing strip_cookies swaps.
        c6 = manager.configure_source(
            "pixiv",
            proxy_mode="custom",
            proxy_url="127.0.0.1:9090",
            strip_cookies=False,
            image_headers={"Referer": "https://www.pixiv.net/", "User-Agent": "PixivTest"},
            timeout=20.0,
        )
        assert c6 is not c5

        # Changing image_headers swaps.
        c7 = manager.configure_source(
            "pixiv",
            proxy_mode="custom",
            proxy_url="127.0.0.1:9090",
            strip_cookies=False,
            image_headers={"Referer": "https://other.example/"},
            timeout=20.0,
        )
        assert c7 is not c6

        # Changing timeout swaps.
        c8 = manager.configure_source(
            "pixiv",
            proxy_mode="custom",
            proxy_url="127.0.0.1:9090",
            strip_cookies=False,
            image_headers={"Referer": "https://other.example/"},
            timeout=45.0,
        )
        assert c8 is not c7
        assert manager._retired == [c1, c3, c4, c5, c6, c7]
        assert manager.get_image_downloader("pixiv")._http_client is c8
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_source_http_manager_strip_cookies_and_lifecycle_closing():
    seen_headers: list[dict[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            headers=[
                ("Set-Cookie", "sid=abc123; Path=/"),
                ("Set-Cookie", "prefs=dark; Path=/"),
            ],
            text="ok",
        )

    manager = SourceHttpManager()
    try:
        stripped = manager.configure_source("stripped", strip_cookies=True)
        await stripped._transport.aclose()
        stripped._transport = httpx.MockTransport(_handler)

        stripped.cookies.set("jar_cookie", "jar_val")
        resp_stripped = await stripped.get(
            "https://example.test/api",
            headers={"Cookie": "explicit=val", "X-Keep": "yes"},
        )
        assert resp_stripped.status_code == 200
        assert "cookie" not in seen_headers[0]
        assert seen_headers[0]["x-keep"] == "yes"
        assert dict(stripped.cookies) == {}
        assert dict(resp_stripped.cookies) == {}

        # Control case: strip_cookies=False preserves outbound cookies and jar updates.
        normal = manager.configure_source("normal", strip_cookies=False)
        await normal._transport.aclose()
        normal._transport = httpx.MockTransport(_handler)

        normal.cookies.set("jar_cookie", "jar_val")
        await normal.get("https://example.test/api")
        assert seen_headers[1]["cookie"] == "jar_cookie=jar_val"
        assert dict(normal.cookies) == {"jar_cookie": "jar_val", "sid": "abc123", "prefs": "dark"}

        # Retire one client while keeping active clients open.
        retired_client = stripped
        active_swapped = manager.configure_source("stripped", strip_cookies=False)
        assert manager._retired == [retired_client]
        assert not retired_client.is_closed
        assert not active_swapped.is_closed
        assert not normal.is_closed

        await manager.aclose_retired()
        assert retired_client.is_closed is True
        assert manager._retired == []
        assert active_swapped.is_closed is False
        assert normal.is_closed is False

        # Retire another client, then verify aclose() closes both retired and active clients.
        active_newest = manager.configure_source("stripped", strip_cookies=True)
        assert manager._retired == [active_swapped]

        await manager.aclose()
        assert active_swapped.is_closed is True
        assert active_newest.is_closed is True
        assert normal.is_closed is True
        assert manager._retired == []
        assert manager._entries == {}
    finally:
        await manager.aclose()


# ---------------------------------------------------------------------------
# 6. PixivSourcePlugin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "allow_bare_id", "expected_id"),
    [
        ("https://www.pixiv.net/novel/show.php?id=12345", False, "12345"),
        ("http://pixiv.net/novel/show.php?id=99&lang=ja", False, "99"),
        ("https://www.pixiv.net/novel/show.php/?id=555", False, "555"),
        ("pixiv:42", False, "42"),
        ("  PIXIV:42  ", False, "42"),
        ("12345", False, None),
        ("12345", True, "12345"),
        ("", True, None),
        ("   ", True, None),
        ("0", True, None),
        ("pixiv:0", True, None),
        ("https://www.pixiv.net/novel/show.php?id=0", True, None),
        ("-5", True, None),
        ("pixiv:abc", False, None),
        ("https://www.pixiv.net/artworks/12345", False, None),
        ("https://www.pixiv.net/novel/series/12345", False, None),
        ("https://www.pixiv.net/novel/show.php", False, None),
        ("https://www.pixiv.net/novel/show.php?id=abc", False, None),
        ("https://evil-pixiv.net/novel/show.php?id=12345", False, None),
        ("ftp://www.pixiv.net/novel/show.php?id=12345", False, None),
    ],
)
def test_pixiv_source_plugin_parse_ref(raw: str, allow_bare_id: bool, expected_id: str | None):
    plugin = PixivSourcePlugin()
    parsed = plugin.parse_ref(raw, allow_bare_id=allow_bare_id)
    if expected_id is None:
        assert parsed is None
    else:
        assert parsed == ParsedSourceRef(
            source=SOURCE_PIXIV,
            kind="chapter",
            series_key="",
            chapter_key=expected_id,
            normalized_ref=f"https://www.pixiv.net/novel/show.php?id={expected_id}",
        )


def test_pixiv_source_plugin_build_external_url():
    plugin = PixivSourcePlugin()
    cfg = SourceRuntimeConfig(source=SOURCE_PIXIV)

    assert plugin.build_external_url("novel", "12345", cfg) == "https://www.pixiv.net/novel/show.php?id=12345"
    assert plugin.build_external_url("series", "678", cfg) == "https://www.pixiv.net/novel/series/678"
    assert plugin.build_external_url("author", "999", cfg) == "https://www.pixiv.net/users/999"
    assert plugin.build_external_url("novel", "", cfg) is None
    assert plugin.build_external_url(cast(Any, "unknown"), "12345", cfg) is None


@pytest.mark.asyncio
async def test_pixiv_source_plugin_on_reload_updates_pool_and_client():
    http_manager = SourceHttpManager(global_proxy_url="127.0.0.1:7890")
    try:
        initial_client = http_manager.get_client(SOURCE_PIXIV)
        pool = AccountPool(http_client=initial_client, min_interval=2.0, cooling_duration=120.0)
        client = PixivClient(account_pool=pool, max_concurrency=5)
        plugin = PixivSourcePlugin(http_manager=http_manager, account_pool=pool, client=client)

        runtime = SourceRuntimeConfig.from_values(
            SOURCE_PIXIV,
            True,
            {
                "proxy_mode": "custom",
                "proxy_url": "127.0.0.1:1080",
                "min_interval": 3.5,
                "cooling_duration": 300.0,
                "max_concurrency": 8,
            },
        )
        credentials = [
            _make_credential(2, name="second", refresh_token="tok_2", sort_index=20, premium=True),
            _make_credential(1, name="first", refresh_token="tok_1", sort_index=10, is_follow=True),
            _make_credential(3, name="disabled", refresh_token="tok_disabled", valid=False, sort_index=5),
            _make_credential(4, name="blank_tok", refresh_token="   ", sort_index=1),
            _make_credential(5, name="missing_tok", refresh_token=None, sort_index=2),
        ]

        plugin.on_reload(runtime, credentials)

        reloaded_http = http_manager.get_client(SOURCE_PIXIV)
        assert reloaded_http is not initial_client
        assert http_manager._retired == [initial_client]
        assert http_manager._entries[SOURCE_PIXIV].spec.proxy == "http://127.0.0.1:1080"
        assert http_manager._entries[SOURCE_PIXIV].spec.strip_cookies is True
        assert http_manager.get_image_downloader(SOURCE_PIXIV)._default_headers == PIXIV_IMAGE_HEADERS

        assert pool._http_client is reloaded_http
        assert pool._min_interval == 3.5
        assert pool._cooling_duration == 300.0
        assert [acc.username for acc in pool.accounts] == ["first", "second"]

        first_acc, second_acc = pool.accounts
        assert first_acc.token_info.token == "tok_1"
        assert first_acc.token_info.follow is True
        assert first_acc.token_info.premium is False
        assert first_acc.min_interval == 3.5
        assert first_acc.cooling_duration == 300.0
        assert first_acc._http_client is reloaded_http

        assert second_acc.token_info.token == "tok_2"
        assert second_acc.token_info.premium is True
        assert second_acc.token_info.follow is False
        assert client.max_concurrency == 8

        # Subsequent reload preserves existing PixivAccount object when token matches and updates settings.
        runtime_next = SourceRuntimeConfig.from_values(
            SOURCE_PIXIV,
            True,
            {
                "proxy_mode": "direct",
                "min_interval": 1.0,
                "cooling_duration": 45.0,
                "max_concurrency": 0,
            },
        )
        plugin.on_reload(
            runtime_next,
            [
                _make_credential(
                    1,
                    name="first_renamed",
                    refresh_token="tok_1",
                    sort_index=1,
                    premium=True,
                    is_follow=False,
                )
            ],
        )

        next_http = http_manager.get_client(SOURCE_PIXIV)
        assert next_http is not reloaded_http
        assert pool._http_client is next_http
        assert pool._min_interval == 1.0
        assert pool._cooling_duration == 45.0
        assert pool.accounts == [first_acc]
        assert first_acc.username == "first_renamed"
        assert first_acc.token_info.premium is True
        assert first_acc.token_info.follow is False
        assert first_acc._http_client is next_http
        assert client.max_concurrency == 1
    finally:
        await http_manager.aclose()


# ---------------------------------------------------------------------------
# 7. LocalSourcePlugin
# ---------------------------------------------------------------------------


def test_local_source_plugin_parse_upload_txt_and_epub():
    parsed_txt = parse_upload("story.txt", "  第一行\r\n第二行  ".encode())
    assert parsed_txt == ParsedUpload(
        body="  第一行\r\n第二行  ", title=None, author_name=None, original=None
    )
    assert normalize_text(parsed_txt.body) == "第一行\n第二行"

    with pytest.raises(UploadUnreadable, match=DECODE_ERROR):
        parse_upload("bad.txt", b"\x80\x81\xff\xfe\x00\x01")

    epub_bytes = make_simple_epub("嵌入书名", "嵌入作者", "第一章 正文")
    parsed_epub = parse_upload("book.epub", epub_bytes)
    assert parsed_epub.title == "嵌入书名"
    assert parsed_epub.author_name == "嵌入作者"
    assert "第一章 正文" in parsed_epub.body
    assert parsed_epub.original == epub_bytes

    with pytest.raises(UploadUnreadable):
        parse_upload("corrupt.epub", b"not a real epub")


def test_local_source_plugin_parse_filename_and_resolve_metadata_toggles():
    # parse_filename_metadata=True extracts author/title and strips trailing status markers.
    assert parse_filename("【猫耳】星辰大海 (完结).txt") == ("星辰大海", "猫耳")
    assert parse_filename("[Author] Tale [txt].TXT") == ("Tale", "Author")
    assert parse_filename("folder\\作者 - 长篇小说 (连载).txt") == ("长篇小说", "作者")
    assert parse_filename("无作者书名.txt") == ("无作者书名", UNKNOWN_AUTHOR)
    assert parse_filename("无作者书名.txt", default_unknown_author="  匿名作者  ") == (
        "无作者书名",
        "匿名作者",
    )
    assert parse_filename("无作者书名.txt", default_unknown_author="   ") == ("无作者书名", UNKNOWN_AUTHOR)
    assert parse_filename(".txt") == (UNNAMED_TITLE, UNKNOWN_AUTHOR)

    # parse_filename_metadata=False preserves stem verbatim and uses fallback author.
    assert parse_filename("【猫耳】星辰大海 (完结).txt", parse_filename_metadata=False) == (
        "【猫耳】星辰大海 (完结)",
        UNKNOWN_AUTHOR,
    )
    assert parse_filename(
        "作者 - 长篇小说 (连载).txt",
        parse_filename_metadata=False,
        default_unknown_author="路人甲",
    ) == ("作者 - 长篇小说 (连载)", "路人甲")
    assert parse_filename(
        ".txt",
        parse_filename_metadata=False,
        default_unknown_author="   ",
    ) == (UNNAMED_TITLE, UNKNOWN_AUTHOR)

    # resolve_metadata respects toggles for TXT, and prioritizes non-blank EPUB metadata.
    txt_payload = ParsedUpload(body="正文")
    assert resolve_metadata(
        "【猫耳】星辰大海 (完结).txt",
        txt_payload,
        parse_filename_metadata=True,
        default_unknown_author="路人甲",
    ) == ("星辰大海", "猫耳")
    assert resolve_metadata(
        "【猫耳】星辰大海 (完结).txt",
        txt_payload,
        parse_filename_metadata=False,
        default_unknown_author="路人甲",
    ) == ("【猫耳】星辰大海 (完结)", "路人甲")

    epub_payload = ParsedUpload(body="正文", title="EPUB书名", author_name="EPUB作者")
    assert resolve_metadata(
        "【猫耳】星辰大海.epub",
        epub_payload,
        parse_filename_metadata=False,
        default_unknown_author="路人甲",
    ) == ("EPUB书名", "EPUB作者")

    partial_epub = ParsedUpload(body="正文", title="EPUB书名", author_name="   ")
    assert resolve_metadata(
        "纯标题.epub",
        partial_epub,
        parse_filename_metadata=False,
        default_unknown_author="自定义未知作者",
    ) == ("EPUB书名", "自定义未知作者")


def test_local_source_plugin_validate_config_and_manifest():
    plugin = LocalSourcePlugin()

    assert plugin.manifest.source == SOURCE_LOCAL
    assert plugin.manifest.has_capability(SourceCapability.FILE_UPLOAD) is True
    assert plugin.manifest.allow_proxy_config is False
    assert plugin.manifest.epub_policy is EpubPolicy.CONTAINER_ONLY

    assert plugin.validate_config({}) == {
        "parse_filename_metadata": True,
        "default_unknown_author": UNKNOWN_AUTHOR,
    }
    assert plugin.validate_config(
        {"parse_filename_metadata": "false", "default_unknown_author": "  无名氏  "}
    ) == {
        "parse_filename_metadata": False,
        "default_unknown_author": "无名氏",
    }

    for blank in ("", "   ", None):
        validated = plugin.validate_config({"default_unknown_author": blank})
        assert validated["default_unknown_author"] == UNKNOWN_AUTHOR

    with pytest.raises(DomainValidationError):
        plugin.validate_config({"parse_filename_metadata": "not-a-bool"})
