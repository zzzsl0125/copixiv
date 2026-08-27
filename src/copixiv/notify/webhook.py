"""Webhook notifier — the second notification backend (docs/MODULARITY.md §M6).

A plain-HTTP-POST channel: task results are delivered as a JSON body to a
user-configured URL.  Enabled by adding ``"webhook"`` to ``notifiers.enabled``
and setting ``webhook.url`` in config.yaml.
"""

from __future__ import annotations

import json

import httpx

from copixiv.core.models import TaskResult
from copixiv.log import logger


class WebhookNotifier:
    """POSTs task results as JSON to a configured webhook URL.

    An empty URL silently disables the backend (same skip semantics as an
    unconfigured Telegram token).
    """

    name = "webhook"

    def __init__(self, url: str = "", timeout: float = 10.0):
        self._url = (url or "").strip()
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def send_task_result(
        self,
        task_name: str,
        status: str,
        duration: float | None = None,
        error: str | None = None,
        result: TaskResult | None = None,
    ) -> None:
        if not self._url:
            logger.debug("Webhook notifier: no URL configured — skipping.")
            return

        payload = {
            "task_name": task_name,
            "status": status,
            "duration": duration,
            "error": error,
            "result": result.model_dump() if result is not None else None,
        }
        response = await self._get_client().post(
            self._url,
            content=json.dumps(payload, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        logger.info(
            "Webhook notifier: task '%s' delivered (%s).", task_name, status,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
