"""Telegram notifier — pure message formatting/splitting + httpx client with tenacity."""

from __future__ import annotations

import asyncio
import html
import re
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

_HTML_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|pre)\b[^>]*>")


def _escape(text: str) -> str:
    """Escape user-provided text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


def _normalize_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    cleaned = proxy_url.strip()
    if not cleaned:
        return None
    if "://" not in cleaned:
        return f"http://{cleaned}"
    return cleaned


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
    if status != "success":
        text = "\n".join(
            [
                "❌ <b>Task Failed</b>",
                f"<b>Task:</b> <code>{_escape(task_name)}</code>",
                "<b>Status:</b> failed",
                f"<b>Error:</b> <code>{_escape(error or 'Unknown error')}</code>",
            ]
        )
        return TelegramMessagePayload(text=text)

    dur = f"{duration:.2f}s" if duration is not None else "N/A"
    titles = result.new_novel_titles if result else []
    count = result.new_novel_count if result else 0
    summary = result.summary if result else ""

    lines = [
        "✅ <b>Task Completed</b>",
        f"<b>Task:</b> <code>{_escape(task_name)}</code>",
        "<b>Status:</b> success",
        f"<b>Duration:</b> <code>{dur}</code>",
    ]

    if titles:
        lines.append(f"<b>New Novels:</b> <code>{count}</code>")
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
        lines.append(f"<b>Result:</b> {_escape(summary)}")
    else:
        lines.append(f"<b>New Novels:</b> <code>{count}</code>")
    return TelegramMessagePayload(text="\n".join(lines))


def _update_tag_stack(stack: list[str], fragment: str) -> None:
    for match in _HTML_TAG_RE.finditer(fragment):
        is_close, tag = match.group(1), match.group(2)
        if not is_close:
            stack.append(tag)
        else:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx] == tag:
                    stack.pop(idx)
                    break


def _safe_cut_index(text: str, limit: int) -> int:
    """Avoid splitting inside an HTML tag ``<...>`` or entity ``&...;``."""
    if len(text) <= limit:
        return len(text)
    cut = limit
    lt = text.rfind("<", 0, cut)
    gt = text.rfind(">", 0, cut)
    if lt > gt:
        cut = lt
    amp = text.rfind("&", 0, cut)
    semi = text.rfind(";", 0, cut)
    if amp > semi and (cut - amp) <= 10:
        cut = amp
    return cut if cut > 0 else limit


def split_telegram_text(
    text: str,
    max_length: int = TELEGRAM_MAX_MESSAGE_LEN,
) -> list[str]:
    """Split *text* into chunks of at most *max_length* while keeping HTML balanced."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    stack: list[str] = []
    remaining = text

    while remaining:
        prefix = "".join(f"<{tag}>" for tag in stack)
        known_close_len = sum(len(f"</{tag}>") for tag in stack)
        # Reserve room for existing closing tags plus a possible new open tag
        # inside this chunk, never exceeding half of max_length.
        reserve = min(known_close_len + 14, max(max_length // 2, known_close_len))
        budget = max(max_length - len(prefix) - reserve, 1)

        if len(remaining) <= max_length - len(prefix) - known_close_len:
            candidate_stack = list(stack)
            _update_tag_stack(candidate_stack, remaining)
            candidate_suffix = "".join(f"</{tag}>" for tag in reversed(candidate_stack))
            if len(prefix) + len(remaining) + len(candidate_suffix) <= max_length:
                chunks.append(f"{prefix}{remaining}{candidate_suffix}")
                break

        split_at = remaining.rfind("\n", 0, budget + 1)
        if split_at > 0:
            chunk_body = remaining[:split_at]
            rest = remaining[split_at + 1 :]
        else:
            cut = _safe_cut_index(remaining, budget)
            chunk_body = remaining[:cut]
            rest = remaining[cut:]

        next_stack = list(stack)
        _update_tag_stack(next_stack, chunk_body)
        suffix = "".join(f"</{tag}>" for tag in reversed(next_stack))

        # Safety trim if chunk_body opened extra tags that pushed past max_length
        while len(prefix) + len(chunk_body) + len(suffix) > max_length and len(chunk_body) > 1:
            trim = max(1, len(prefix) + len(chunk_body) + len(suffix) - max_length)
            new_len = _safe_cut_index(chunk_body, max(len(chunk_body) - trim, 1))
            rest = chunk_body[new_len:] + rest
            chunk_body = chunk_body[:new_len]
            next_stack = list(stack)
            _update_tag_stack(next_stack, chunk_body)
            suffix = "".join(f"</{tag}>" for tag in reversed(next_stack))

        stack = next_stack
        remaining = rest
        chunks.append(f"{prefix}{chunk_body}{suffix}")

    return chunks


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

    name = "telegram"

    def __init__(
        self,
        token: str = "",
        chat_id: str | int = "",
        proxy_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        max_attempts: int = 3,
    ) -> None:
        self._token: str = token
        self._chat_id: str = str(chat_id) if chat_id else ""
        self._message_url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        self._document_url = f"https://api.telegram.org/bot{self._token}/sendDocument"
        self._proxy_url = _normalize_proxy_url(proxy_url)
        self._client = http_client
        self._owns_client = http_client is None
        self._max_attempts = max(max_attempts, 1)

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
            caption_chunks = split_telegram_text(
                payload.text,
                max_length=TELEGRAM_MAX_CAPTION_LEN,
            )
            await self._send_document(
                caption_chunks[0],
                payload.document_content,
                payload.document_name,
            )
            if len(caption_chunks) > 1:
                overflow = "\n".join(caption_chunks[1:])
                for msg_chunk in split_telegram_text(overflow, TELEGRAM_MAX_MESSAGE_LEN):
                    await self._send_message(msg_chunk)
            return

        for msg_chunk in split_telegram_text(payload.text, TELEGRAM_MAX_MESSAGE_LEN):
            await self._send_message(msg_chunk)

    async def close(self) -> None:
        """Close the underlying HTTP client if owned by this notifier."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(proxy=self._proxy_url)
            self._owns_client = True
        return self._client

    async def _post_with_retry(
        self,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> None:
        client = self._get_client()

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
    "split_telegram_text",
]
