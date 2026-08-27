"""Notifier assembly — plain config-driven mapping, no registry.

``config.notifications`` lists backend entries (each ``type`` is a
``Literal["telegram", "webhook"]``); this module maps each entry to its
implementation (docs/MODULARITY.md §M6).  It is deliberately a plain
function and not a plugin registry: the only backends are the two built
in here, and a third-party channel has never existed.  If one ever does,
revisit this decision then (MODULARITY.md §6).
"""

from __future__ import annotations

from copixiv.log import logger

from .telegram import TelegramNotifier
from .webhook import WebhookNotifier


def build_notifiers(config) -> list[TelegramNotifier | WebhookNotifier]:
    """Build the notification backends from *config*.

    Each ``config.notifications`` entry selects one backend by ``type``.
    Unknown names are logged loudly and skipped (never fatal) — a
    defensive path, since ``NotificationBackendConfig.type`` is a
    ``Literal`` that would already fail validation on a bad value.
    """
    backends: list[TelegramNotifier | WebhookNotifier] = []
    for backend in config.notifications:
        if backend.type == "telegram":
            backends.append(TelegramNotifier(
                token=backend.token,
                chat_id=backend.chat_id,
                proxy_http=config.proxy.url or None,
            ))
        elif backend.type == "webhook":
            backends.append(WebhookNotifier(url=backend.url))
        else:
            logger.warning("Unknown notifier backend '{}' — skipped.", backend.type)
    return backends
