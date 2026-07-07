"""Telegram notifier — sends task results via Telegram Bot API.

Port of V1's ``core/notifier.py`` adapted to V2's async architecture.
Uses ``httpx.AsyncClient`` for non-blocking HTTP calls.

Uses Telegram's **HTML** parse mode (instead of Markdown) because task
names, novel titles, and summary text can contain characters that break
Markdown parsing (``*``, ``_``, ``[``, etc.).  HTML only requires escaping
``&``, ``<``, ``>``, which is handled by :func:`_escape`.
"""

from __future__ import annotations

import html
from datetime import datetime

import httpx

from copixiv.app.config import AppConfig
from copixiv.domain.models.task_result import TaskResult
from copixiv.app.logger import logger


def _escape(text: str) -> str:
    """Escape user-provided text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


class TelegramNotifier:
    """Sends task-completion notifications to a Telegram chat.

    Requires ``telegram.token`` and ``telegram.chat_id`` in the app config.
    If either is missing, notifications are silently skipped.

    Maintains a single ``httpx.AsyncClient`` instance for connection reuse
    across multiple notifications.  Call ``close()`` when done.
    """

    def __init__(self, config: AppConfig) -> None:
        tg = config.telegram
        self._token: str = tg.token
        self._chat_id: str = str(tg.chat_id) if tg.chat_id else ""
        self._message_url = (
            f"https://api.telegram.org/bot{self._token}/sendMessage"
        )
        self._document_url = (
            f"https://api.telegram.org/bot{self._token}/sendDocument"
        )

        proxy_http = config.proxy.http
        if proxy_http and "://" not in proxy_http:
            proxy_http = f"http://{proxy_http}"
        self._proxies: str | None = proxy_http if proxy_http else None

        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_task_result(
        self,
        task_name: str,
        status: str,
        duration: float | None = None,
        error: str | None = None,
        result: TaskResult | None = None,
    ) -> None:
        """Send a formatted task-completion notification via Telegram."""
        if not self._token or not self._chat_id:
            logger.debug("Telegram not configured — skipping notification.")
            return

        if status == "success":
            await self._send_success(task_name, duration, result)
        else:
            await self._send_failure(task_name, error or "Unknown error")

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared client, creating it lazily on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(proxy=self._proxies)
        return self._client

    async def _send_success(
        self,
        task_name: str,
        duration: float | None,
        result: TaskResult | None,
    ) -> None:
        dur = f"{duration:.2f}s" if duration is not None else "N/A"
        titles = result.new_novel_titles if result else []
        count = result.new_novel_count if result else 0
        summary = result.summary if result else ""

        lines = [
            f"✅ <b>Task Completed</b>",
            f"<b>Task:</b> <code>{_escape(task_name)}</code>",
            f"<b>Status:</b> success",
            f"<b>Duration:</b> <code>{dur}</code>",
        ]

        if titles:
            lines.append(f"<b>New Novels:</b> <code>{count}</code>")
            text = "\n".join(lines)
            if len(titles) > 10:
                file_content = "\n".join(titles)
                file_name = (
                    f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                )
                await self._send_document(text, file_content, file_name)
            else:
                lines.append("<b>Novels:</b>")
                for title in titles:
                    lines.append(f"- <code>{_escape(title)}</code>")
                await self._send_message("\n".join(lines))
        elif summary:
            lines.append(f"<b>Result:</b> {_escape(summary)}")
            await self._send_message("\n".join(lines))
        else:
            lines.append(f"<b>New Novels:</b> <code>{count}</code>")
            await self._send_message("\n".join(lines))

    async def _send_failure(self, task_name: str, error: str) -> None:
        text = "\n".join([
            f"❌ <b>Task Failed</b>",
            f"<b>Task:</b> <code>{_escape(task_name)}</code>",
            f"<b>Status:</b> failed",
            f"<b>Error:</b> <code>{_escape(error)}</code>",
        ])
        await self._send_message(text)

    async def _send_message(self, text: str) -> None:
        """POST a text message to the Telegram Bot API (HTML parse mode)."""
        try:
            client = self._get_client()
            resp = await client.post(
                self._message_url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Telegram notification sent successfully.")
        except Exception as exc:
            logger.error("Failed to send Telegram message: {}", exc)

    async def _send_document(
        self, caption: str, file_content: str, file_name: str
    ) -> None:
        """Send a text document attachment via the Telegram Bot API."""
        try:
            client = self._get_client()
            resp = await client.post(
                self._document_url,
                data={
                    "chat_id": self._chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={
                    "document": (
                        file_name,
                        file_content.encode("utf-8"),
                        "text/plain",
                    )
                },
                timeout=20,
            )
            resp.raise_for_status()
            logger.info("Telegram document sent successfully.")
        except Exception as exc:
            logger.error("Failed to send Telegram document: {}", exc)
