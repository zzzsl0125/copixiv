"""Layer 1 unit tests: Telegram formatting/chunking/retry and bounded log capture."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest
import respx

from copixiv.core.models import TaskResult
from copixiv.log import capture_logs, logger, setup_logging
from copixiv.notify.factory import build_notifier
from copixiv.notify.telegram import (
    TELEGRAM_MAX_CAPTION_LEN,
    TELEGRAM_MAX_MESSAGE_LEN,
    TelegramNotifier,
    format_task_notification,
    split_telegram_text,
)


def test_split_telegram_text_preserves_html_tags_and_length_bounds():
    long_line = "<b>" + ("A" * 50) + "</b>"
    chunks = split_telegram_text(long_line, max_length=25)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 25
        assert chunk.startswith("<b>")
        assert chunk.endswith("</b>")

    multi_line = "\n".join(f"- <code>Title {i}</code>" for i in range(300))
    msg_chunks = split_telegram_text(multi_line, max_length=TELEGRAM_MAX_MESSAGE_LEN)
    assert len(msg_chunks) >= 2
    assert all(len(c) <= TELEGRAM_MAX_MESSAGE_LEN for c in msg_chunks)


def test_format_task_notification_switches_to_document_above_10_titles():
    small = format_task_notification(
        "novel_follow",
        "success",
        duration=1.5,
        result=TaskResult(summary="ok", new_novel_titles=["<b>Book1</b>"]),
    )
    assert small.document_content is None
    assert "&lt;b&gt;Book1&lt;/b&gt;" in small.text

    many = format_task_notification(
        "novel_follow",
        "success",
        duration=2.0,
        result=TaskResult(
            summary="ok",
            new_novel_titles=[f"Book {i}" for i in range(12)],
        ),
    )
    assert many.document_content is not None
    assert many.document_name is not None
    assert many.document_name.endswith(".txt")
    assert len(many.document_content.splitlines()) == 12


@pytest.mark.asyncio
async def test_telegram_notifier_retries_429_splits_long_messages_and_masks_token(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _no_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr("copixiv.notify.telegram.asyncio.sleep", _no_sleep)
    token = "123456:SECRET_BOT_TOKEN"

    async with httpx.AsyncClient() as http_client:
        notifier = TelegramNotifier(
            token=token,
            chat_id="9999",
            http_client=http_client,
            max_attempts=3,
        )

        with respx.mock:
            msg_route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
                side_effect=[
                    httpx.Response(429, json={"parameters": {"retry_after": 1}}),
                    httpx.Response(200, json={"ok": True}),
                    httpx.Response(200, json={"ok": True}),
                ]
            )
            # Send a huge failure error > 4096 chars -> splits and retries 429
            await notifier.send_task_result(
                "huge_task",
                "failed",
                error="X" * 5000,
            )
            assert msg_route.call_count == 3

        # 400 error is NOT retried and token is masked in logged error
        captured: list[str] = []
        sink_id = logger.add(lambda m: captured.append(str(m)), level="ERROR")
        try:
            with respx.mock:
                bad_route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
                    return_value=httpx.Response(400, text="Bad Request")
                )
                await notifier.send_task_result("t", "failed", error="bad")
                assert bad_route.call_count == 1
        finally:
            logger.remove(sink_id)
        assert any("***" in line and token not in line for line in captured)

        # Document with > 1024 caption spills remainder to sendMessage
        with respx.mock:
            doc_route = respx.post(f"https://api.telegram.org/bot{token}/sendDocument").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            overflow_route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            await notifier.send_task_result(
                "t_" + ("N" * 1200),
                "success",
                result=TaskResult(
                    summary="ok",
                    new_novel_titles=[f"Book {i}" for i in range(15)],
                ),
            )
            assert doc_route.call_count == 1
            assert overflow_route.call_count >= 1
            assert TELEGRAM_MAX_CAPTION_LEN == 1024

        # Factory builds notifier with proxy_url and shared http_client
        cfg = SimpleConfigStub(token=token, chat_id="1", proxy_url="127.0.0.1:8080")
        built = build_notifier(cfg, http_client=http_client)
        assert built is not None
        assert built._proxy_url == "http://127.0.0.1:8080"


class SimpleConfigStub:
    def __init__(self, token: str, chat_id: str, proxy_url: str) -> None:
        self.notifications = [type("N", (), {"token": token, "chat_id": chat_id})()]
        self.proxy = type("P", (), {"url": proxy_url})()


def test_capture_logs_bounded_ring_buffer_thread_safe_and_post_exit(tmp_path: Path):
    import contextvars

    setup_logging(log_dir=str(tmp_path))

    # Bounded ring buffer truncates head while preserving tail and works post-exit
    with capture_logs(task_id=77, max_bytes=200) as get_logs:

        def _worker(idx: int) -> None:
            for j in range(10):
                logger.info(f"worker-{idx}-line-{j:02d}-" + ("Z" * 20))

        threads = [
            threading.Thread(
                target=contextvars.copy_context().run,
                args=(_worker, i),
            )
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    output = get_logs()
    assert "日志过长" in output
    # Tail lines retained; bounded well below total emitted (~2000 bytes)
    body = output.split("\n", 1)[1]
    assert len(body.encode("utf-8")) <= 200
    assert get_logs() == output

    # Single oversized line also stays within cap
    with capture_logs(task_id=88, max_bytes=128) as get_big:
        logger.info("B" * 1000)
    big_out = get_big()
    assert "日志过长" in big_out
    assert len(big_out.split("\n", 1)[1].encode("utf-8")) <= 128
