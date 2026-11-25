"""Telegram notifier — pure message formatting + httpx client with tenacity."""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)

from copixiv.core.models import TaskResult
from copixiv.log import logger

TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_CAPTION_LEN = 1024

_MAX_TASK_NAME_LEN = 120
_MAX_DETAIL_LEN = 3000
_TRUNCATED_NOTE = "…（已截断，请在任务历史查看完整信息）"


def _escape(text: str) -> str:
    """Escape user-provided text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


def _truncate(text: str, limit: int) -> str:
    """Truncate *text* to *limit* chars, appending a task-history note when cut."""
    if len(text) <= limit:
        return text
    keep = max(limit - len(_TRUNCATED_NOTE), 0)
    return text[:keep] + _TRUNCATED_NOTE


@dataclass(frozen=True)
class TelegramMessagePayload:
    """Formatted notification ready for transport."""

    text: str
    document_content: str | None = None
    document_name: str | None = None


def format_task_notification(
    task_name: str,
    status: str,
    duration: float | None = None,
    error: str | None = None,
    result: TaskResult | None = None,
) -> TelegramMessagePayload:
    """Render a :class:`TaskResult` or failure notice into Telegram HTML payload."""
    safe_task_name = _escape(_truncate(task_name, _MAX_TASK_NAME_LEN))
    if status != "success":
        safe_error = _escape(_truncate(error or "Unknown error", _MAX_DETAIL_LEN))
        text = "\n".join(
            [
                "❌ <b>Task Failed</b>",
                f"<b>Task:</b> <code>{safe_task_name}</code>",
                "<b>Status:</b> failed",
                f"<b>Error:</b> <code>{safe_error}</code>",
            ]
        )
        return TelegramMessagePayload(text=text)

    dur = f"{duration:.2f}s" if duration is not None else "N/A"
    titles = result.new_novel_titles if result else []
    count = result.new_novel_count if result else 0
    summary = result.summary if result else ""
    failed_count = len(result.failed_ids) if result else 0

    lines = [
        "✅ <b>Task Completed</b>",
        f"<b>Task:</b> <code>{safe_task_name}</code>",
        "<b>Status:</b> success",
        f"<b>Duration:</b> <code>{dur}</code>",
    ]

    if titles:
        lines.append(f"<b>New Novels:</b> <code>{count}</code>")
        if failed_count:
            lines.append(f"<b>Failed:</b> <code>{failed_count}</code>")
        text = "\n".join(lines)
        if len(titles) > 10:
            from copixiv.core.naming import safe_filename

            file_content = "\n".join(titles)
            file_name = safe_filename(f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}") + ".txt"
            return TelegramMessagePayload(
                text=text,
                document_content=file_content,
                document_name=file_name,
            )
        lines.append("<b>Novels:</b>")
        for title in titles:
            lines.append(f"- <code>{_escape(title)}</code>")
        return TelegramMessagePayload(text="\n".join(lines))

    if summary:
        lines.append(f"<b>Result:</b> {_escape(_truncate(summary, _MAX_DETAIL_LEN))}")
    else:
        lines.append(f"<b>New Novels:</b> <code>{count}</code>")
    return TelegramMessagePayload(text="\n".join(lines))


class _RetryableTelegramError(RuntimeError):
    def __init__(self, msg: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(msg)


def _extract_retry_after(resp: httpx.Response) -> float | None:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            params = payload.get("parameters")
            if isinstance(params, dict) and params.get("retry_after") is not None:
                return min(max(float(params["retry_after"]), 0.1), 60.0)
    except ValueError:
        pass
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(max(float(header.strip()), 0.1), 60.0)
        except ValueError:
            pass
    return None


class TelegramNotifier:
    """Sends task-completion notifications to a Telegram chat."""

    def __init__(
        self,
        token: str,
        chat_id: str | int,
        http_client: httpx.AsyncClient,
        *,
        max_attempts: int = 3,
    ) -> None:
        self._token: str = token
        self._chat_id: str = str(chat_id) if chat_id else ""
        self._message_url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        self._document_url = f"https://api.telegram.org/bot{self._token}/sendDocument"
        self._client = http_client
        self._max_attempts = max(max_attempts, 1)

    @classmethod
    def from_config(
        cls,
        config: Any,
        http_client: httpx.AsyncClient,
    ) -> TelegramNotifier | None:
        """Build a :class:`TelegramNotifier` from *config*, or ``None`` when unconfigured."""
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
        if not entry.token or not entry.chat_id:
            return None

        return cls(
            token=entry.token,
            chat_id=entry.chat_id,
            http_client=http_client,
        )

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

        payload = format_task_notification(
            task_name=task_name,
            status=status,
            duration=duration,
            error=error,
            result=result,
        )
        if payload.document_content is not None and payload.document_name is not None:
            await self._send_document(
                payload.text[:TELEGRAM_MAX_CAPTION_LEN],
                payload.document_content,
                payload.document_name,
            )
            return

        await self._send_message(payload.text[:TELEGRAM_MAX_MESSAGE_LEN])

    async def _post_with_retry(
        self,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> None:
        client = self._client

        def _wait(retry_state: RetryCallState) -> float:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            if isinstance(exc, _RetryableTelegramError) and exc.retry_after is not None:
                return exc.retry_after
            attempt = max(retry_state.attempt_number - 1, 0)
            return min(1.0 * (2**attempt), 16.0)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            retry=retry_if_exception_type(
                (_RetryableTelegramError, httpx.TransportError, httpx.TimeoutException)
            ),
            wait=_wait,
            sleep=asyncio.sleep,
            reraise=True,
        ):
            with attempt:
                resp = await client.post(
                    url,
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    retry_after = _extract_retry_after(resp)
                    raise _RetryableTelegramError(
                        "Telegram 429 rate limit",
                        retry_after=retry_after,
                    )
                if resp.status_code >= 500:
                    raise _RetryableTelegramError(f"Telegram HTTP {resp.status_code}")
                resp.raise_for_status()

    async def _send_message(self, text: str) -> None:
        try:
            await self._post_with_retry(
                self._message_url,
                json_body={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10.0,
            )
            logger.info("Telegram notification sent successfully.")
        except Exception as exc:
            logger.error(
                "Failed to send Telegram message: {}",
                str(exc).replace(self._token, "***") if self._token else str(exc),
            )

    async def _send_document(
        self,
        caption: str,
        file_content: str,
        file_name: str,
    ) -> None:
        try:
            await self._post_with_retry(
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
                timeout=20.0,
            )
            logger.info("Telegram document sent successfully.")
        except Exception as exc:
            logger.error(
                "Failed to send Telegram document: {}",
                str(exc).replace(self._token, "***") if self._token else str(exc),
            )


__all__ = [
    "TELEGRAM_MAX_CAPTION_LEN",
    "TELEGRAM_MAX_MESSAGE_LEN",
    "TelegramMessagePayload",
    "TelegramNotifier",
    "format_task_notification",
]
