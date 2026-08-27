"""Notifier assembly tests (docs/MODULARITY.md §M6).

Covers the config-driven factory (``notifiers.enabled`` → backends) and
the backend behaviors it selects: webhook delivery semantics and
composite fault isolation.
"""

import json
from types import SimpleNamespace

import httpx

from copixiv.config import AppConfig
from copixiv.core.models import TaskResult
from copixiv.notify.composite import CompositeNotifier
from copixiv.notify.factory import build_notifiers
from copixiv.notify.telegram import TelegramNotifier
from copixiv.notify.webhook import WebhookNotifier


def _config(enabled: list[str]) -> AppConfig:
    """Build an AppConfig (new ``notifications`` list shape) for *enabled*."""
    notifications = []
    for name in enabled:
        if name == "telegram":
            notifications.append(
                {"type": "telegram", "token": "1:TOK", "chat_id": "42"}
            )
        elif name == "webhook":
            notifications.append({"type": "webhook", "url": "http://x.test/h"})
    return AppConfig(notifications=notifications)


def test_enabled_maps_to_backends():
    backends = build_notifiers(_config(["telegram", "webhook"]))
    assert [type(b) for b in backends] == [TelegramNotifier, WebhookNotifier]
    assert backends[0]._token == "1:TOK"
    assert backends[1]._url == "http://x.test/h"


def test_empty_disables_notifications():
    assert build_notifiers(_config([])) == []


def test_unknown_name_is_skipped_with_warning():
    from copixiv.log import capture_logs

    # An unknown backend type cannot pass ``NotificationBackendConfig``'s
    # ``Literal`` validation, so exercise the factory's defensive skip by
    # feeding it a raw config object (as if assembled by hand).
    config = SimpleNamespace(
        proxy=SimpleNamespace(url=""),
        notifications=[
            SimpleNamespace(type="telegram", token="1:TOK", chat_id="42", url=""),
            SimpleNamespace(type="nope", token="t", chat_id="1", url=""),
        ],
    )
    with capture_logs() as get_logs:
        backends = build_notifiers(config)
        logs = get_logs()  # buffer closes when the context exits

    assert len(backends) == 1
    assert isinstance(backends[0], TelegramNotifier)
    assert "Unknown notifier backend 'nope'" in logs


class _RecordingBackend:
    """Minimal backend double for composite tests."""

    def __init__(self, name="rec"):
        self.name = name
        self.sent: list[dict] = []
        self.closed = False

    async def send_task_result(self, **kwargs):
        self.sent.append(kwargs)
        if self.name == "boom":
            raise RuntimeError("boom")

    async def close(self):
        self.closed = True


def test_webhook_backend_skips_without_url():
    notifier = WebhookNotifier(url="")
    result = TaskResult(summary="x")

    async def run():
        await notifier.send_task_result("t", "success", result=result)
        await notifier.close()

    import asyncio
    asyncio.run(run())
    # no URL → no client created, nothing sent


def test_webhook_backend_posts_json():
    class _FakeClient:
        def __init__(self):
            self.calls: list[dict] = []

        async def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            return httpx.Response(
                200, request=httpx.Request("POST", url),
            )

        async def aclose(self):
            pass

    notifier = WebhookNotifier(url="http://example.test/hook")
    fake = _FakeClient()
    notifier._get_client = lambda: fake
    result = TaskResult(summary="完成", new_novel_titles=["小说一"])

    async def run():
        await notifier.send_task_result(
            "novel_fetch", "success", duration=1.2, result=result,
        )
        await notifier.close()

    import asyncio
    asyncio.run(run())

    assert len(fake.calls) == 1
    payload = json.loads(fake.calls[0]["content"])
    assert payload["task_name"] == "novel_fetch"
    assert payload["status"] == "success"
    assert payload["result"]["new_novel_titles"] == ["小说一"]


def test_composite_fans_out_and_isolates_failures():
    a = _RecordingBackend("a")
    b = _RecordingBackend("b")
    boom = _RecordingBackend("boom")
    composite = CompositeNotifier([a, boom, b])
    result = TaskResult(summary="s")

    async def run():
        await composite.send_task_result("t", "success", result=result)
        await composite.close()

    import asyncio
    asyncio.run(run())

    assert len(a.sent) == 1
    assert len(b.sent) == 1  # boom 抛异常不影响 b
    assert a.closed and b.closed and boom.closed
