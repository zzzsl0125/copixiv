"""Notifier assembly — plain config-driven mapping, no registry.

Telegram is the only notification backend.  ``config.notifications`` keeps
its list shape for future channels, but only the first entry is used: this
module maps it to its implementation (docs/MODULARITY.md §M6).  It is
deliberately a plain function and not a plugin registry: no third-party
channel has ever existed.  If one ever does, revisit this decision then
(MODULARITY.md §6).
"""

from __future__ import annotations

from copixiv.log import logger

from .telegram import TelegramNotifier


def build_notifier(config) -> TelegramNotifier | None:
    """Build the notification backend from *config*.

    Returns ``None`` when ``config.notifications`` is empty (notifications
    disabled).  Otherwise the first entry is mapped to a
    :class:`TelegramNotifier`; any additional entries are ignored with a
    warning, since there is no fan-out anymore.
    """
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
        proxy_http=config.proxy.url or None,
    )
