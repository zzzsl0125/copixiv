"""Telegram notifier main paths — message building + skip-when-unconfigured.

The C2 regression test pins token redaction on error logs; these pin the
notification payloads themselves (success / failure / document mode).
"""

import httpx

from copixiv.app.config import AppConfig
from copixiv.domain.models.task_result import TaskResult
from copixiv.infrastructure.notifier.telegram import TelegramNotifier


class _FakeClient:
    """Records post() calls and returns canned responses."""

    def __init__(self, status_code: int = 200):
        self._status = status_code
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return httpx.Response(self._status)


def _notifier(fake_client) -> TelegramNotifier:
    config = AppConfig(telegram={"token": "123:TOKEN", "chat_id": "42"})
    notifier = TelegramNotifier(config)
    notifier._get_client = lambda: fake_client
    return notifier


async def test_success_message_contains_summary_and_titles():
    fake = _FakeClient()
    notifier = _notifier(fake)
    result = TaskResult(summary="下载完成: 2 本", new_novel_titles=["小说一", "小说二"])

    await notifier.send_task_result(
        task_name="novel_fetch", status="success", duration=1.25, result=result,
    )

    assert len(fake.calls) == 1
    payload = fake.calls[0]["json"]
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"
    assert "Task Completed" in payload["text"]
    assert "novel_fetch" in payload["text"]
    assert "小说一" in payload["text"] and "小说二" in payload["text"]


async def test_failure_message_contains_error():
    fake = _FakeClient()
    notifier = _notifier(fake)

    await notifier.send_task_result(
        task_name="rebuild_fts", status="failed", error="index corrupt",
    )

    assert len(fake.calls) == 1
    text = fake.calls[0]["json"]["text"]
    assert "Task Failed" in text
    assert "index corrupt" in text


async def test_more_than_ten_titles_sends_document():
    fake = _FakeClient()
    notifier = _notifier(fake)
    titles = [f"标题{i}" for i in range(11)]
    result = TaskResult(summary="x", new_novel_titles=titles)

    await notifier.send_task_result(
        task_name="novel_search", status="success", duration=2.0, result=result,
    )

    assert len(fake.calls) == 1
    files = fake.calls[0]["files"]
    assert "document" in files
    file_name, content, _media_type = files["document"]
    assert file_name.startswith("novel_search_") and file_name.endswith(".txt")
    assert b"\n".join(t.encode("utf-8") for t in titles) in content


async def test_unconfigured_notifier_skips_without_network():
    config = AppConfig(telegram={"token": "", "chat_id": ""})
    notifier = TelegramNotifier(config)
    fake = _FakeClient()
    notifier._get_client = lambda: fake

    await notifier.send_task_result(task_name="x", status="success")

    assert fake.calls == []
