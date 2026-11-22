"""Layer 1 unit tests: Telegram formatting/chunking/retry and bounded log capture."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest
import respx

from copixiv.core.models import TaskResult
from copixiv.log import capture_logs, logger, setup_logging
from copixiv.notify.telegram import (
    TELEGRAM_MAX_CAPTION_LEN,
    TELEGRAM_MAX_MESSAGE_LEN,
    TelegramNotifier,
    format_task_notification,
)


def test_format_task_notification_truncates_long_task_name_and_error():
    payload = format_task_notification(
        "t_" + ("N" * 300),
        "failed",
        error="X" * 5000,
    )
    assert len(payload.text) <= TELEGRAM_MAX_MESSAGE_LEN
    assert "任务历史" in payload.text
    assert payload.text.endswith("</code>")


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
                ]
            )
            # Huge failure error -> truncated with history note and retries 429
            await notifier.send_task_result(
                "huge_task",
                "failed",
                error="X" * 5000,
            )
            assert msg_route.call_count == 2

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

        # Document with long task_name keeps caption <= TELEGRAM_MAX_CAPTION_LEN
        with respx.mock:
            doc_route = respx.post(f"https://api.telegram.org/bot{token}/sendDocument").mock(
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
            assert TELEGRAM_MAX_CAPTION_LEN == 1024

        # TelegramNotifier.from_config builds notifier when configured and returns None when empty
        cfg = SimpleConfigStub(token=token, chat_id="1")
        built = TelegramNotifier.from_config(cfg, http_client)
        assert built is not None
        assert TelegramNotifier.from_config(SimpleConfigStub(token="", chat_id="1"), http_client) is None
        assert TelegramNotifier.from_config(SimpleConfigStub(token=token, chat_id=""), http_client) is None


class SimpleConfigStub:
    def __init__(self, token: str, chat_id: str) -> None:
        self.notifications = [type("N", (), {"token": token, "chat_id": chat_id})()]


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
