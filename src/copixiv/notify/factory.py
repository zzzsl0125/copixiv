"""Notifier assembly — plain config-driven mapping, no registry."""

from __future__ import annotations

import httpx

from copixiv.log import logger

from .telegram import TelegramNotifier


def build_notifier(
    config,
    http_client: httpx.AsyncClient | None = None,
) -> TelegramNotifier | None:
    """Build the notification backend from *config*."""
    entries = config.notifications
    if not entries:
        return None

    if len(entries) > 1:
        logger.warning(
            "Multiple notification entries configured ({}) — only the first "
            "Telegram entry is used; the list shape is retained for future "
            "channels.",
            len(entries),
        )

    entry = entries[0]
    return TelegramNotifier(
        token=entry.token,
        chat_id=entry.chat_id,
        proxy_url=config.proxy.url or None,
        http_client=http_client,
    )
