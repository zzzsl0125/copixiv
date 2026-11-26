"""Multi-source plugin package — registry, HTTP manager, and built-in sources."""

from __future__ import annotations

from copixiv.sources.base import BaseSourcePlugin
from copixiv.sources.http import SourceHttpManager
from copixiv.sources.local import LocalSourcePlugin
from copixiv.sources.pixiv import PixivSourcePlugin
from copixiv.sources.registry import SourceRegistry

__all__ = [
    "BaseSourcePlugin",
    "LocalSourcePlugin",
    "PixivSourcePlugin",
    "SourceHttpManager",
    "SourceRegistry",
]
