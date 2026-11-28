"""Layer 1 unit tests: ESJZone parser, HTTP client, password unlock, WAF guard, and plugin."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
import pytest
import respx

from copixiv.core.exceptions import ValidationError as DomainValidationError
from copixiv.core.models import SOURCE_ESJ
from copixiv.core.sources import (
    EpubPolicy,
    ExternalAuthor,
    ParsedSourceRef,
    SourceCapability,
    SourceCredentialRecord,
    SourcePlugin,
    SourceRuntimeConfig,
)
from copixiv.sources.esj import (
    DEFAULT_ESJ_BASE_URL,
    EsjChallengeError,
    EsjClient,
    EsjNotFoundError,
    EsjPasswordRequiredError,
    EsjRequestError,
    EsjSourcePlugin,
    build_esj_author_key,
    extract_jinjing_token,
    is_challenge_html,
    is_original_genre,
    parse_book_outline,
    parse_chapter,
    parse_esj_count,
    parse_esj_datetime,
    parse_esj_ref,
)
from copixiv.sources.http import SourceHttpManager
from copixiv.sources.registry import SourceRegistry

_UTC_PLUS_8 = timezone(timedelta(hours=8))


def _make_runtime(
    *,
    base_url: str = DEFAULT_ESJ_BASE_URL,
    proxy_mode: str = "direct",
    proxy_url: str = "",
    request_interval_sec: float = 0.0,
    chapter_passwords: dict[str, str] | None = None,
    download_images: bool = True,
) -> SourceRuntimeConfig:
    return SourceRuntimeConfig.from_values(
        SOURCE_ESJ,
        True,
        {
            "base_url": base_url,
            "proxy_mode": proxy_mode,
            "proxy_url": proxy_url,
            "request_interval_sec": request_interval_sec,
            "chapter_passwords": chapter_passwords or {},
            "download_images": download_images,
        },
    )


def _make_cookie_cred(
    cred_id: int = 1,
    *,
    name: str = "cookie_acc",
    cookie: str = "ws_key=abc; PHPSESSID=xyz",
    valid: bool = True,
    sort_index: int = 0,
) -> SourceCredentialRecord:
    return SourceCredentialRecord(
        id=cred_id,
        source=SOURCE_ESJ,
        name=name,
        kind="cookie",
        secret={"cookie": cookie},
        valid=valid,
        sort_index=sort_index,
    )


def _make_password_cred(
    cred_id: int = 2,
    *,
    name: str = "pw_acc",
    username: str = "reader@example.com",
    password: str = "secret123",
    valid: bool = True,
    sort_index: int = 0,
) -> SourceCredentialRecord:
    return SourceCredentialRecord(
        id=cred_id,
        source=SOURCE_ESJ,
        name=name,
        kind="password",
        secret={"username": username, "password": password},
        valid=valid,
        sort_index=sort_index,
    )


# ---------------------------------------------------------------------------
# 1. Manifest, Config Validation, URLs, and Ref Parsing
# ---------------------------------------------------------------------------


def test_esj_manifest_and_protocol_conformance():
    plugin = EsjSourcePlugin()
    assert isinstance(plugin, SourcePlugin)
    m = plugin.manifest

    assert m.source == SOURCE_ESJ
    assert m.display_name == "ESJZone"
    assert m.badge_label == "ESJ"
    assert m.badge_class == "bg-indigo-100 text-indigo-800"
    assert m.enabled_by_default is False
    assert m.epub_policy is EpubPolicy.BEST_EFFORT_EXTERNAL
    assert m.allow_proxy_config is True
    assert m.editable_fields == ("series_index", "tags", "create_time")
    assert m.capabilities == frozenset(
        {
            SourceCapability.FETCH_BY_REF,
            SourceCapability.SYNC_NOVEL,
            SourceCapability.RETRY_FAILURE,
            SourceCapability.AUTH_POOL,
            SourceCapability.TEST_CONNECTION,
        }
    )
    assert m.shelf is not None
    assert m.shelf.section_id == "esj"
    assert m.shelf.keyword == "source:esj;"
    assert m.shelf.intake_action == "ref_fetch"
    assert [s.key for s in m.settings_schema] == [
        "base_url",
        "proxy_mode",
        "proxy_url",
        "request_interval_sec",
        "chapter_passwords",
        "download_images",
        "task_timeout_sec",
    ]
    assert [c.kind for c in m.credential_kinds] == ["cookie", "password"]


def test_esj_validate_config_and_build_external_url():
    plugin = EsjSourcePlugin()

    defaults = plugin.validate_config({})
    assert defaults == {
        "base_url": "https://www.esjzone.cc",
        "proxy_mode": "direct",
        "proxy_url": "",
        "request_interval_sec": 1.0,
        "chapter_passwords": {},
        "download_images": True,
        "task_timeout_sec": 7200,
    }

    custom = plugin.validate_config(
        {
            "base_url": "www.esjzone.one/",
            "proxy_mode": "custom",
            "proxy_url": "127.0.0.1:7890",
            "request_interval_sec": "2.5",
            "chapter_passwords": {" 1660702902/999 ": "pw123", "*": "fallback"},
            "download_images": "false",
        }
    )
    assert custom["base_url"] == "https://www.esjzone.one"
    assert custom["proxy_url"] == "http://127.0.0.1:7890"
    assert custom["request_interval_sec"] == 2.5
    assert custom["chapter_passwords"] == {"1660702902/999": "pw123", "*": "fallback"}
    assert custom["download_images"] is False

    assert plugin.validate_config({"base_url": "   "})["base_url"] == "https://www.esjzone.cc"

    with pytest.raises(DomainValidationError):
        plugin.validate_config({"base_url": "ftp://www.esjzone.cc"})

    with pytest.raises(DomainValidationError):
        plugin.validate_config({"proxy_mode": "custom", "proxy_url": ""})

    with pytest.raises(DomainValidationError):
        plugin.validate_config({"chapter_passwords": {"": "pw"}})

    cfg = _make_runtime(base_url="https://www.esjzone.me/")
    assert plugin.build_external_url("series", "12345", cfg) == "https://www.esjzone.me/detail/12345.html"
    assert (
        plugin.build_external_url("novel", "12345/678", cfg) == "https://www.esjzone.me/forum/12345/678.html"
    )
    assert plugin.build_external_url("author", "uid:42", cfg) == "https://www.esjzone.me/my/profile?uid=42"
    assert plugin.build_external_url("author", "name:山田", cfg) is None
    assert plugin.build_external_url("novel", "bad_key", cfg) is None
    assert plugin.build_external_url("series", "", cfg) is None
    assert plugin.build_external_url(cast(Any, "other"), "123", cfg) is None


@pytest.mark.parametrize(
    ("raw", "allow_bare_id", "expected"),
    [
        (
            "https://www.esjzone.cc/detail/1660702902.html",
            False,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="book",
                series_key="1660702902",
                chapter_key=None,
                normalized_ref="https://www.esjzone.cc/detail/1660702902.html",
            ),
        ),
        (
            "http://esjzone.one/detail/123.html?ref=1",
            False,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="book",
                series_key="123",
                chapter_key=None,
                normalized_ref="https://www.esjzone.cc/detail/123.html",
            ),
        ),
        (
            "https://m.esjzone.me/forum/123/456.html#post",
            False,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="chapter",
                series_key="123",
                chapter_key="456",
                normalized_ref="https://www.esjzone.cc/forum/123/456.html",
            ),
        ),
        (
            "https://esjzone.cc/forum/123/456.html/",
            False,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="chapter",
                series_key="123",
                chapter_key="456",
                normalized_ref="https://www.esjzone.cc/forum/123/456.html",
            ),
        ),
        (
            "esj:789",
            False,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="book",
                series_key="789",
                chapter_key=None,
                normalized_ref="https://www.esjzone.cc/detail/789.html",
            ),
        ),
        (
            "  ESJ:789/1011  ",
            False,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="chapter",
                series_key="789",
                chapter_key="1011",
                normalized_ref="https://www.esjzone.cc/forum/789/1011.html",
            ),
        ),
        ("789", False, None),
        ("789/1011", False, None),
        (
            "789",
            True,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="book",
                series_key="789",
                chapter_key=None,
                normalized_ref="https://www.esjzone.cc/detail/789.html",
            ),
        ),
        (
            "789/1011",
            True,
            ParsedSourceRef(
                source=SOURCE_ESJ,
                kind="chapter",
                series_key="789",
                chapter_key="1011",
                normalized_ref="https://www.esjzone.cc/forum/789/1011.html",
            ),
        ),
        ("0", True, None),
        ("esj:0", True, None),
        ("esj:123/0", True, None),
        ("https://www.esjzone.cc/detail/0.html", False, None),
        ("https://evil-esjzone.cc/detail/123.html", False, None),
        ("https://www.esjzone.cc.evil.com/detail/123.html", False, None),
        ("ftp://www.esjzone.cc/detail/123.html", False, None),
        ("https://www.esjzone.cc/tags/fantasy.html", False, None),
        ("", True, None),
        ("   ", True, None),
    ],
)
def test_esj_parse_ref(raw: str, allow_bare_id: bool, expected: ParsedSourceRef | None):
    plugin = EsjSourcePlugin()
    assert parse_esj_ref(raw, allow_bare_id=allow_bare_id) == expected
    assert plugin.parse_ref(raw, allow_bare_id=allow_bare_id) == expected


def test_esj_registry_parse_ref_routing():
    registry = SourceRegistry([EsjSourcePlugin()])
    assert registry.parse_ref("12345") is None
    assert registry.parse_ref("12345/678") is None
    assert registry.parse_ref("12345", preferred_source=SOURCE_ESJ) == ParsedSourceRef(
        source=SOURCE_ESJ,
        kind="book",
        series_key="12345",
        normalized_ref="https://www.esjzone.cc/detail/12345.html",
    )
    assert registry.parse_ref("12345/678", preferred_source=SOURCE_ESJ) == ParsedSourceRef(
        source=SOURCE_ESJ,
        kind="chapter",
        series_key="12345",
        chapter_key="678",
        normalized_ref="https://www.esjzone.cc/forum/12345/678.html",
    )


# ---------------------------------------------------------------------------
# 2. Pure HTML Parsing (Book Outline & Chapter)
# ---------------------------------------------------------------------------


def test_author_key_and_count_datetime_helpers():
    assert is_original_genre("原創") is True
    assert is_original_genre("原创") is True
    assert is_original_genre("日輕") is False
    assert is_original_genre("韓輕") is False
    assert is_original_genre("原創(譯)") is False
    assert is_original_genre("") is False

    assert build_esj_author_key("原創作者", uid="88", genre="原創") == "uid:88"
    assert build_esj_author_key("原創作者", uid=None, genre="原創") == "name:原創作者"
    assert build_esj_author_key("山田太郎", uid="88", genre="日輕") == "name:山田太郎"
    assert build_esj_author_key("", uid=None, genre="韓輕") == "name:佚名"
    assert build_esj_author_key(None, uid="99", is_original=True) == "uid:99"

    assert parse_esj_count("12,345") == 12345
    assert parse_esj_count("1.5K") == 1500
    assert parse_esj_count("2.35w") == 23500
    assert parse_esj_count("1.2萬") == 12000
    assert parse_esj_count("--") == 0

    assert parse_esj_datetime("發佈於 2026-05-01 18:30:45") == datetime(
        2026, 5, 1, 18, 30, 45, tzinfo=_UTC_PLUS_8
    )
    assert parse_esj_datetime("2026/5/9") == datetime(2026, 5, 9, 0, 0, 0, tzinfo=_UTC_PLUS_8)
    assert parse_esj_datetime("invalid-date") is None


def test_parse_book_outline_extracts_metadata_dedup_tags_nested_and_external_chapters():
    html = """
    <html>
      <body>
        <div class="row">
          <div class="product-gallery">
            <img src="data:image/gif;base64,R0lGOD" data-src="/uploads/covers/book_100.jpg" alt="cover">
          </div>
          <div class="book-detail">
            <h2>  異世界轉生冒險譚 &amp; 記錄  </h2>
            <ul class="list-unstyled">
              <li>類型：日輕</li>
              <li>作者: <a href="/tags/author/">山田太郎</a></li>
            </ul>
            <div>
              <span id="vtimes">12.5k</span>
              <span id="favorite">1,234</span>
            </div>
          </div>
        </div>
        <section class="widget widget-tags d-none d-md-block">
          <a href="/tags/1/">奇幻</a>
          <a href="/tags/2/">異世界</a>
          <a href="/tags/3/">冒險</a>
        </section>
        <section class="widget widget-tags d-md-none">
          <a href="/tags/1/">奇幻</a>
          <a href="/tags/4/">R18</a>
          <a href="/tags/2/">異世界</a>
        </section>
        <div id="chapterList">
          <details open>
            <summary>第一卷</summary>
            <a href="https://www.esjzone.cc/forum/100/1001.html"><p>序章 啟程</p></a>
            <a href="/forum/100/1002.html">第一章 遇見</a>
          </details>
          <a href="https://ncode.syosetu.com/n9999aa/" target="_blank">日文生肉</a>
          <a href="https://www.esjzone.one/forum/100/1003.html">第二章 番外</a>
          <a href="https://www.esjzone.cc/forum/999/8888.html">其他書本外鏈</a>
          <a href="#">無效錨點</a>
        </div>
      </body>
    </html>
    """
    outline = parse_book_outline(html, "100", "https://www.esjzone.cc")

    assert outline.series.source_key == "100"
    assert outline.series.title == "異世界轉生冒險譚 & 記錄"
    assert outline.series.cover_url == "https://www.esjzone.cc/uploads/covers/book_100.jpg"
    assert outline.author == ExternalAuthor(source_key="name:山田太郎", name="山田太郎")
    assert outline.view == 12500
    assert outline.like == 1234
    assert outline.tags == ("奇幻", "異世界", "冒險", "R18")
    assert outline.extra["genre"] == "日輕"
    assert outline.extra["is_original"] is False
    assert outline.extra["author_name"] == "山田太郎"

    assert len(outline.chapter_entries) == 5
    c1, c2, c3, c4, c5 = outline.chapter_entries
    assert (c1.source_key, c1.title, c1.series_index, c1.is_external_link) == (
        "100/1001",
        "序章 啟程",
        1,
        False,
    )
    assert (c2.source_key, c2.title, c2.series_index, c2.is_external_link) == (
        "100/1002",
        "第一章 遇見",
        2,
        False,
    )
    assert (c3.title, c3.series_index, c3.is_external_link, c3.external_url) == (
        "日文生肉",
        3,
        True,
        "https://ncode.syosetu.com/n9999aa/",
    )
    assert (c4.source_key, c4.title, c4.series_index, c4.is_external_link) == (
        "100/1003",
        "第二章 番外",
        4,
        False,
    )
    assert (c5.title, c5.series_index, c5.is_external_link, c5.external_url) == (
        "其他書本外鏈",
        5,
        True,
        "https://www.esjzone.cc/forum/999/8888.html",
    )


def test_parse_book_outline_original_with_empty_author_fallback():
    html = """
    <div class="book-detail">
      <h2>無名原創</h2>
      <ul>
        <li>類型: 原創</li>
        <li>作者: </li>
      </ul>
    </div>
    """
    outline = parse_book_outline(html, "200", "https://www.esjzone.cc")
    assert outline.author == ExternalAuthor(source_key="name:佚名", name="佚名")
    assert outline.extra["is_original"] is True


def test_parse_chapter_preserves_traditional_chinese_paragraphs_images_and_meta():
    html = """
    <html>
      <head><title>第一章 標題 - ESJ Zone</title></head>
      <body>
        <nav><h2>導覽列</h2></nav>
        <h2>第一章 繁體測試 &amp; 冒險</h2>
        <div class="single-post-meta">
          <div><a href="/my/profile?uid=54321">譯者小張</a></div>
          <div>發佈時間：2026-08-15 21:05:30</div>
        </div>
        <div class="forum-content">
          <script>alert("ignore me");</script>
          <p>　　這是一段繁體中文，包含「引號」與&lt;特殊符號&gt;。</p>
          <p>第二段第一行<br>
          第二段第二行</p>
          <div>第三段內容&nbsp;續接</div>
          <p><img src="/assets/img/p1.jpg" /></p>
          <p><img src="data:image/png;base64,AAAA" data-src="https://img.example.com/p2.png" /></p>
          <p><img src="data:image/gif;base64,INLINE_ONLY" /></p>
          <p><img data-original="/assets/img/p3.webp" /></p>
        </div>
      </body>
    </html>
    """
    chapter = parse_chapter(
        html,
        book_id="100",
        chapter_id="1001",
        chapter_url="https://www.esjzone.cc/forum/100/1001.html",
        base_url="https://www.esjzone.cc",
    )

    assert chapter.title == "第一章 繁體測試 & 冒險"
    assert chapter.poster_uid == "54321"
    assert chapter.poster_name == "譯者小張"
    assert chapter.create_time == datetime(2026, 8, 15, 21, 5, 30, tzinfo=_UTC_PLUS_8)
    assert chapter.requires_password is False
    assert chapter.images == {
        "1": "https://www.esjzone.cc/assets/img/p1.jpg",
        "2": "https://img.example.com/p2.png",
        "3": "https://www.esjzone.cc/assets/img/p3.webp",
    }
    assert chapter.content == (
        "　　這是一段繁體中文，包含「引號」與<特殊符號>。\n\n"
        "第二段第一行\n"
        "第二段第二行\n\n"
        "第三段內容 續接\n\n"
        "[uploadedimage:1]\n\n"
        "[uploadedimage:2]\n\n"
        "[uploadedimage:3]"
    )
    assert "alert" not in chapter.content


def test_parse_chapter_detects_password_and_challenge_markers():
    locked_html = """
    <div class="forum-content">
      <p>內文目前施工中，請輸入密碼</p>
      <button id="btn-send-pw">送出</button>
    </div>
    """
    parsed = parse_chapter(
        locked_html,
        "100",
        "1001",
        "https://www.esjzone.cc/forum/100/1001.html",
        "https://www.esjzone.cc",
    )
    assert parsed.requires_password is True
    assert extract_jinjing_token("ok <JinJing>tok-abc-123</JinJing> end") == "tok-abc-123"
    assert extract_jinjing_token("<JinJing>  </JinJing>") is None

    assert is_challenge_html('<div class="cf-turnstile" data-sitekey="x"></div>') is True
    assert is_challenge_html("<title>Just a moment...</title>") is True
    assert is_challenge_html('<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>') is True
    assert is_challenge_html('<input type="hidden" name="__cf_chl_tk" value="x">') is True
    assert (
        is_challenge_html('<div class="forum-content"><p>He said "Just a moment" quietly.</p></div>') is False
    )


# ---------------------------------------------------------------------------
# 3. HTTP Client, Password Unlock, WAF Abort, Rate Limiting, and Plugin Flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esj_password_unlock_flow_and_loop_guard():
    http_manager = SourceHttpManager()
    try:
        plugin = EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0)
        locked_html = '<div class="forum-content">內文目前施工中 <button id="btn-send-pw">解鎖</button></div>'
        unlocked_html = (
            "<h2>第二章 已解鎖</h2>"
            '<div class="single-post-meta"><a href="/my/profile?uid=77">原創者</a><span>2026-09-01 10:00:00</span></div>'
            '<div class="forum-content"><p>解鎖後正文內容</p></div>'
        )

        # 1) Missing password raises EsjPasswordRequiredError without unlock POSTs.
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/forum/10/20.html").mock(
                return_value=httpx.Response(200, text=locked_html)
            )
            with pytest.raises(EsjPasswordRequiredError):
                await plugin.fetch_chapter("10", "20", _make_runtime(), ())

        # 2) Valid password from chapter_passwords priority (book/chapter > book > *) succeeds.
        runtime_pw = _make_runtime(
            chapter_passwords={"10/20": "exact_pw", "10": "book_pw", "*": "wildcard_pw"}
        )
        with respx.mock(assert_all_called=True) as mock:
            get_route = mock.get("https://www.esjzone.cc/forum/10/20.html").mock(
                side_effect=[
                    httpx.Response(200, text=locked_html),
                    httpx.Response(200, text=unlocked_html),
                ]
            )
            token_route = mock.post("https://www.esjzone.cc/forum/20/10.html").mock(
                return_value=httpx.Response(200, text="<JinJing>jin-token-999</JinJing>")
            )
            pw_route = mock.post("https://www.esjzone.cc/inc/forum_pw.php").mock(
                return_value=httpx.Response(
                    200,
                    text="ok",
                    headers={"Set-Cookie": "esj_pw=unlocked; Path=/"},
                )
            )

            chapter = await plugin.fetch_chapter("10", "20", runtime_pw, ())

            assert get_route.call_count == 2
            assert token_route.call_count == 1
            assert b"plxf=getAuthToken" in token_route.calls[0].request.content
            assert pw_route.call_count == 1
            assert pw_route.calls[0].request.headers["authorization"] == "jin-token-999"
            assert b"pw=exact_pw" in pw_route.calls[0].request.content
            assert "esj_pw=unlocked" in get_route.calls[1].request.headers.get("cookie", "")
            assert chapter.title == "第二章 已解鎖"
            assert chapter.content == "解鎖後正文內容"
            assert chapter.author == ExternalAuthor(source_key="uid:77", name="原創者")

        # 3) Loop guard: if re-fetched GET still shows btn-send-pw, stop after 1 attempt.
        with respx.mock(assert_all_called=True) as mock:
            get_route = mock.get("https://www.esjzone.cc/forum/10/21.html").mock(
                side_effect=[
                    httpx.Response(200, text=locked_html),
                    httpx.Response(200, text=locked_html),
                ]
            )
            token_route = mock.post("https://www.esjzone.cc/forum/21/10.html").mock(
                return_value=httpx.Response(200, text="<JinJing>tok-2</JinJing>")
            )
            pw_route = mock.post("https://www.esjzone.cc/inc/forum_pw.php").mock(
                return_value=httpx.Response(200, text="wrong")
            )

            with pytest.raises(EsjPasswordRequiredError, match="仍处于锁定状态"):
                await plugin.fetch_chapter("10", "21", _make_runtime(), (), password="wrong_pw")

            assert get_route.call_count == 2
            assert token_route.call_count == 1
            assert pw_route.call_count == 1

        # 4) Missing JinJing token raises EsjPasswordRequiredError cleanly.
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/forum/10/22.html").mock(
                return_value=httpx.Response(200, text=locked_html)
            )
            mock.post("https://www.esjzone.cc/forum/22/10.html").mock(
                return_value=httpx.Response(200, text="no token here")
            )
            with pytest.raises(EsjPasswordRequiredError, match="JinJing"):
                await plugin.fetch_chapter("10", "22", _make_runtime(), (), password="pw")
    finally:
        await http_manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (403, "<html>Forbidden</html>"),
        (503, "<html>Service Unavailable</html>"),
        (200, '<html><div class="cf-turnstile"></div></html>'),
        (200, "<html><title>Just a moment...</title></html>"),
        (200, "<html><script src='/cdn-cgi/challenge-platform/x'></script></html>"),
        (200, "<html>__cf_chl_opt</html>"),
    ],
)
async def test_esj_waf_and_challenge_abort_immediately(status_code: int, body: str):
    http_manager = SourceHttpManager()
    try:
        plugin = EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0)
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get("https://www.esjzone.cc/detail/300.html").mock(
                return_value=httpx.Response(status_code, text=body)
            )
            with pytest.raises(EsjChallengeError):
                await plugin.fetch_book_outline("300", _make_runtime(), ())
            assert route.call_count == 1
    finally:
        await http_manager.aclose()


@pytest.mark.asyncio
async def test_esj_404_raises_not_found():
    http_manager = SourceHttpManager()
    try:
        plugin = EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0)
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/detail/404.html").mock(
                return_value=httpx.Response(404, text="Not Found")
            )
            with pytest.raises(EsjNotFoundError):
                await plugin.fetch_book_outline("404", _make_runtime(), ())
    finally:
        await http_manager.aclose()


@pytest.mark.asyncio
async def test_esj_test_connection_logged_in_anonymous_and_challenge():
    http_manager = SourceHttpManager()
    try:
        plugin = EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0)
        runtime = _make_runtime()
        cookie_cred = _make_cookie_cred()

        # 1) Logged-in 200 profile page
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(
                    200,
                    text='<html><a href="/my/profile?uid=777">書友甲</a><a href="/my/logout">登出</a></html>',
                )
            )
            res = await plugin.test_connection(runtime, [cookie_cred])
            assert res.ok is True
            assert res.latency_ms is not None
            assert res.latency_ms >= 0
            assert "已登录" in res.message
            assert res.account_summary == "書友甲 (uid:777)"
            assert route.calls[0].request.headers["cookie"] == "ws_key=abc; PHPSESSID=xyz"

        # 2) Anonymous 302 redirect to login without credentials -> ok=True (anonymous mode)
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(302, headers={"Location": "/my/login"})
            )
            res = await plugin.test_connection(runtime, ())
            assert res.ok is True
            assert res.account_summary == "匿名访问"

        # 3) 302 redirect to login WITH credential -> ok=False (expired/invalid credential)
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(302, headers={"Location": "/my/login"})
            )
            res = await plugin.test_connection(runtime, [cookie_cred])
            assert res.ok is False
            assert "未登录" in res.message

        # 4) Password credential logs in via POST /inc/mem_login.php then checks /my/profile
        pw_cred = _make_password_cred()
        with respx.mock(assert_all_called=True) as mock:
            login_route = mock.post("https://www.esjzone.cc/inc/mem_login.php").mock(
                return_value=httpx.Response(
                    200,
                    json={"status": 200, "msg": "登入成功"},
                    headers={"Set-Cookie": "esj_sess=sess123; Path=/"},
                )
            )
            prof_route = mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(
                    200,
                    text='<html><a href="/my/profile?uid=888">帳密用戶</a></html>',
                )
            )
            res = await plugin.test_connection(runtime, [pw_cred])
            assert res.ok is True
            assert res.account_summary == "帳密用戶 (uid:888)"
            assert login_route.call_count == 1
            assert "esj_sess=sess123" in prof_route.calls[0].request.headers.get("cookie", "")

        # 5) 200 login form (without redirect) -> ok=True if anonymous, ok=False if credential present
        login_page_html = '<html><form action="/inc/mem_login.php"><input name="pwd"></form></html>'
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(200, text=login_page_html)
            )
            res_anon = await plugin.test_connection(runtime, ())
            assert res_anon.ok is True
            assert res_anon.account_summary == "匿名访问"

        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(200, text=login_page_html)
            )
            res_expired = await plugin.test_connection(runtime, [cookie_cred])
            assert res_expired.ok is False

        # 6) 403 challenge -> ok=False
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.cc/my/profile").mock(
                return_value=httpx.Response(403, text="cf-turnstile")
            )
            res = await plugin.test_connection(runtime, ())
            assert res.ok is False
            assert "403" in res.message or "Cloudflare" in res.message
    finally:
        await http_manager.aclose()


@pytest.mark.asyncio
async def test_esj_on_reload_fetch_novel_for_sync_and_rate_limiting():
    http_manager = SourceHttpManager(global_proxy_url="127.0.0.1:7890")
    try:
        initial_client = http_manager.get_client(SOURCE_ESJ)
        esj_client = EsjClient(http_manager=http_manager, default_request_interval=0.05)
        plugin = EsjSourcePlugin(http_manager=http_manager, client=esj_client)

        runtime = _make_runtime(
            base_url="https://www.esjzone.one/",
            proxy_mode="direct",
            request_interval_sec=0.05,
            download_images=True,
        )
        plugin.on_reload(runtime, ())

        reloaded_client = http_manager.get_client(SOURCE_ESJ)
        assert reloaded_client is not initial_client
        assert http_manager._entries[SOURCE_ESJ].spec.proxy is None
        assert http_manager._entries[SOURCE_ESJ].spec.strip_cookies is False
        assert http_manager.get_image_downloader(SOURCE_ESJ)._default_headers == {
            "Referer": "https://www.esjzone.one/"
        }

        detail_html = """
        <div class="product-gallery"><img src="/cover.jpg"></div>
        <div class="book-detail">
          <h2>原創測試小說</h2>
          <ul><li>類型: 原創</li><li>作者: 原創作者甲</li></ul>
          <span id="vtimes">500</span><span id="favorite">50</span>
        </div>
        <section class="widget-tags"><a href="/t/1">奇幻</a><a href="/t/2">原創</a></section>
        <div id="chapterList">
          <a href="/forum/555/1001.html">第一章</a>
          <a href="/forum/555/1002.html">第二章</a>
        </div>
        """
        chapter_html = """
        <h2>第二章 正式開始</h2>
        <div class="single-post-meta">
          <a href="/my/profile?uid=321">原創作者甲</a>
          <span>2026-07-01 12:00:00</span>
        </div>
        <div class="forum-content"><p>正文段落</p><img src="/img/ch2.jpg"></div>
        """

        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.one/detail/555.html").mock(
                return_value=httpx.Response(200, text=detail_html)
            )
            mock.get("https://www.esjzone.one/forum/555/1002.html").mock(
                return_value=httpx.Response(200, text=chapter_html)
            )
            started = time.monotonic()
            synced = await plugin.fetch_novel_for_sync("555/1002", None, runtime, ())
            elapsed = time.monotonic() - started

            assert elapsed >= 0.04
            assert synced is not None
            assert synced.source_key == "555/1002"
            assert synced.title == "第二章 正式開始"
            assert synced.series_index == 2
            assert synced.author == ExternalAuthor(source_key="uid:321", name="原創作者甲")
            assert synced.like == 50
            assert synced.view == 500
            assert synced.tags == ("奇幻", "原創")
            assert synced.cover_url == "https://www.esjzone.one/cover.jpg"
            assert synced.images == {"1": "https://www.esjzone.one/img/ch2.jpg"}

        # download_images=False omits chapter image map while keeping text clean.
        runtime_no_img = _make_runtime(
            base_url="https://www.esjzone.one",
            request_interval_sec=0.0,
            download_images=False,
        )
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.one/forum/555/1002.html").mock(
                return_value=httpx.Response(200, text=chapter_html)
            )
            ch_no_img = await plugin.fetch_chapter("555", "1002", runtime_no_img, ())
            assert ch_no_img.images == {}

        # Translated book attributes author by name:{detail_author} even if poster uid exists.
        translated_detail_html = """
        <div class="book-detail">
          <h2>譯作小說</h2>
          <ul><li>類型: 日輕</li><li>作者: 田中老師</li></ul>
        </div>
        <div id="chapterList">
          <a href="/forum/777/2001.html">第1話</a>
        </div>
        """
        translated_chapter_html = """
        <h2>第1話 譯文</h2>
        <div class="single-post-meta">
          <a href="/my/profile?uid=999">熱心譯者</a>
          <span>2026/07/02</span>
        </div>
        <div class="forum-content"><p>譯文正文</p></div>
        """
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.one/detail/777.html").mock(
                return_value=httpx.Response(200, text=translated_detail_html)
            )
            mock.get("https://www.esjzone.one/forum/777/2001.html").mock(
                return_value=httpx.Response(200, text=translated_chapter_html)
            )
            synced_tr = await plugin.fetch_novel_for_sync("777/2001", None, runtime_no_img, ())
            assert synced_tr is not None
            assert synced_tr.author == ExternalAuthor(source_key="name:田中老師", name="田中老師")
            assert synced_tr.series_index == 1

        # HTTP 500 surfaces EsjRequestError with status_code.
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.esjzone.one/detail/500.html").mock(
                return_value=httpx.Response(500, text="Server Error")
            )
            with pytest.raises(EsjRequestError) as exc_info:
                await plugin.fetch_book_outline("500", runtime_no_img, ())
            assert exc_info.value.status_code == 500

        # Invalid source_key on fetch_novel_for_sync raises ValidationError.
        with pytest.raises(DomainValidationError):
            await plugin.fetch_novel_for_sync("bad_key", None, runtime_no_img, ())
    finally:
        await http_manager.aclose()


@pytest.mark.asyncio
async def test_esj_duplicate_cookies_in_jar_avoid_cookie_conflict():
    http_manager = SourceHttpManager()
    try:
        plugin = EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0)
        runtime = _make_runtime(base_url="https://www.esjzone.one")
        cookie_cred = _make_cookie_cred(cookie="ews_key=k1; ews_token=t1; PHPSESSID=old")

        client = http_manager.get_client(SOURCE_ESJ)
        client.cookies.set("hidden", "root_val", domain="www.esjzone.one", path="/")
        client.cookies.set("hidden", "dot_val", domain=".esjzone.one", path="/")
        client.cookies.set("hidden", "forum_val", domain="www.esjzone.one", path="/forum")
        client.cookies.set("hidden", "cc_val", domain="www.esjzone.cc", path="/")
        client.cookies.set("PHPSESSID", "new", domain="www.esjzone.one", path="/")

        chapter_html = (
            "<h2>第一章 測試</h2>"
            '<div class="single-post-meta"><a href="/my/profile?uid=77">作者</a></div>'
            '<div class="forum-content"><p>正常正文</p></div>'
        )
        profile_html = '<html><a href="/my/profile?uid=777">書友甲</a><a href="/my/logout">登出</a></html>'

        with respx.mock(assert_all_called=True) as mock:
            chap_route = mock.get("https://www.esjzone.one/forum/100/200.html").mock(
                return_value=httpx.Response(200, text=chapter_html)
            )
            prof_route = mock.get("https://www.esjzone.one/my/profile").mock(
                return_value=httpx.Response(200, text=profile_html)
            )

            chapter = await plugin.fetch_chapter("100", "200", runtime, [cookie_cred])
            assert chapter.title == "第一章 測試"
            assert chapter.content == "正常正文"

            chap_cookie = chap_route.calls[0].request.headers["cookie"]
            assert "ews_key=k1" in chap_cookie
            assert "ews_token=t1" in chap_cookie
            assert "PHPSESSID=new" in chap_cookie
            assert "PHPSESSID=old" not in chap_cookie
            assert "hidden=forum_val" in chap_cookie
            assert "hidden=root_val" in chap_cookie
            assert "hidden=dot_val" in chap_cookie
            assert "cc_val" not in chap_cookie

            res = await plugin.test_connection(runtime, [cookie_cred])
            assert res.ok is True
            assert res.account_summary == "書友甲 (uid:777)"

            prof_cookie = prof_route.calls[0].request.headers["cookie"]
            assert "ews_key=k1" in prof_cookie
            assert "ews_token=t1" in prof_cookie
            assert "PHPSESSID=new" in prof_cookie
            assert "PHPSESSID=old" not in prof_cookie
            assert "hidden=root_val" in prof_cookie
            assert "hidden=dot_val" in prof_cookie
            assert "forum_val" not in prof_cookie
            assert "cc_val" not in prof_cookie
    finally:
        await http_manager.aclose()


def test_esj_on_reload_clears_cookies_on_reused_client():
    http_manager = SourceHttpManager()
    plugin = EsjSourcePlugin(http_manager=http_manager, default_request_interval=0.0)
    runtime = _make_runtime(base_url="https://www.esjzone.one")

    plugin.on_reload(runtime, ())
    first_client = http_manager.get_client(SOURCE_ESJ)
    first_client.cookies.set("hidden", "v1", domain="www.esjzone.one", path="/")
    first_client.cookies.set("hidden", "v2", domain=".esjzone.one", path="/")
    assert len(first_client.cookies.jar) == 2

    # Unchanged runtime reuses the same httpx.AsyncClient, but cookies must still be cleared.
    plugin.on_reload(runtime, [_make_cookie_cred()])
    second_client = http_manager.get_client(SOURCE_ESJ)
    assert second_client is first_client
    assert len(second_client.cookies.jar) == 0
