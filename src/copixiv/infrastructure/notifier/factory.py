"""Notifier assembly — plain config-driven mapping, no registry.

``notifiers.enabled`` lists backend names; this module maps each name to
its implementation (docs/MODULARITY.md §M6).  It is deliberately a plain
function and not a plugin registry: the only backends are the two built
in here, and a third-party channel has never existed.  If one ever does,
revisit this decision then (MODULARITY.md §6).
"""

from __future__ import annotations

from copixiv.domain.ports.notifier import NotifierBackendPort
from copixiv.log import logger

from .telegram import TelegramNotifier
from .webhook import WebhookNotifier

KNOWN_BACKENDS = {"telegram", "webhook"}


def build_notifiers(config) -> list[NotifierBackendPort]:
    """Build the enabled notifier backends from *config*.

    Unknown names are logged loudly and skipped (never fatal), matching
    the old registry's behavior.
    """
    enabled = config.notifiers.enabled
    for name in sorted(set(enabled) - KNOWN_BACKENDS):
        logger.warning("Unknown notifier backend '{}' — skipped.", name)

    backends: list[NotifierBackendPort] = []
    if "telegram" in enabled:
        backends.append(TelegramNotifier(
            token=config.telegram.token,
            chat_id=config.telegram.chat_id,
            proxy_http=config.proxy.http,
        ))
    if "webhook" in enabled:
        backends.append(WebhookNotifier(url=config.webhook.url))
    return backends
