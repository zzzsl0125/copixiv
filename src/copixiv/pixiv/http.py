"""Shared Pixiv HTTP helpers — anonymous (no-auth) image fetching.

Pixiv's image CDN (``i.pximg.net``) serves images with just a browser-like
``Referer`` + UA — no OAuth token.  The API layer (per-account
``AppPixivAPI``) and the image layer therefore share these constants and
helpers while keeping auth strictly on the API side.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# The same mobile UA pixivpy3 sends for API calls.
PIXIV_IOS_USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
# Referer required by i.pximg.net for anonymous image access.
PIXIV_REFERER = "https://www.pixiv.net/"

_RETRYABLE_STATUS = [500, 502, 503, 504]


def create_image_session(
    proxy_http: str = "",
    proxy_https: str = "",
    max_retries: int = 3,
) -> requests.Session:
    """Build a requests session for anonymous image downloads.

    One session per download batch (not per image): connection pooling
    keeps the CDN handshakes down while ``Retry`` absorbs transient 5xx.
    """
    session = requests.Session()
    retries = Retry(
        total=max_retries,
        backoff_factor=0.5,
        status_forcelist=_RETRYABLE_STATUS,
    )
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({
        "Referer": PIXIV_REFERER,
        "User-Agent": PIXIV_IOS_USER_AGENT,
    })
    proxies = {"http": proxy_http, "https": proxy_https}
    if proxy_http or proxy_https:
        session.proxies.update(proxies)
    return session


def pick_image_url(urls, order=("original", "large", "medium", "small")) -> str | None:
    """Pick the best available image URL by preference order.

    Tolerates ``None`` and dicts missing some sizes (the webview API
    omits sizes it does not carry).  Default order is
    original > large > medium > small; callers whose payload lacks a size
    (e.g. linked illustrations without ``large``) pass a narrower order.
    """
    if not isinstance(urls, dict):
        return None
    for key in order:
        url = urls.get(key)
        if url:
            return url
    return None
