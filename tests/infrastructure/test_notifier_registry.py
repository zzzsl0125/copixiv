"""Notifier-backend registry tests (docs/MODULARITY.md §M6 验收).

Pin the plugin contract for notification channels:

1. built-in discovery — backend modules self-register on import;
2. composite fan-out — every enabled backend receives the result, one
   failing backend never affects the others, close() releases all;
3. webhook backend — JSON delivery + skip-when-unconfigured semantics
   (the "second backend" demonstration);
4. config-driven assembly — ``notifiers.enabled`` maps names to
   factories via the registry.
"""

import httpx
import pytest

from copixiv.domain.models.task_result import TaskResult
from copixiv.infrastructure.notifier.composite import CompositeNotifier
from copixiv.infrastructure.notifier.registry import (
    discover_backends,
    get_backend_builder,
    list_backends,
)
from copixiv.infrastructure.notifier.webhook import WebhookNotifier


@pytest.fixture(autouse=True)
def _discover():
    discover_backends()


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


def test_builtin_backends_discovered():
    builders = list_backends()
    assert set(builders) >= {"telegram", "webhook"}


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
    import json
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


def test_backend_factory_reads_notifier_config():
    """telegram/webhook 工厂从 AppConfig 的各自配置节取参数。"""
    from copixiv.app.config import AppConfig

    cfg = AppConfig(
        telegram={"token": "1:TOK", "chat_id": "42"},
        webhook={"url": "http://x.test/h"},
    )
    tg_builder = get_backend_builder("telegram")
    hook_builder = get_backend_builder("webhook")
    assert tg_builder is not None and hook_builder is not None

    tg = tg_builder(cfg)
    hook = hook_builder(cfg)
    assert tg.name == "telegram"
    assert tg._token == "1:TOK"
    assert hook.name == "webhook"
    assert hook._url == "http://x.test/h"


def test_unknown_backend_has_no_builder():
    assert get_backend_builder("nonexistent-channel") is None
