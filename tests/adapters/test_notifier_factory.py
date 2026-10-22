"""Notifier assembly tests (docs/MODULARITY.md §M6).

Covers the config-driven factory: Telegram is the only backend, the first
entry is used, and obsolete ``webhook`` configs fail fast instead of being
silently skipped.
"""

import pytest
from pydantic import ValidationError

from copixiv.config import AppConfig
from copixiv.log import capture_logs
from copixiv.notify.factory import build_notifier
from copixiv.notify.telegram import TelegramNotifier


def _config(entries: list[dict]) -> AppConfig:
    """Build an AppConfig with the ``notifications`` list shape."""
    return AppConfig(notifications=entries)


def test_telegram_entry_builds_notifier_with_proxy():
    config = AppConfig(
        notifications=[{"type": "telegram", "token": "1:TOK", "chat_id": "42"}],
        proxy={"url": "127.0.0.1:20172"},
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, TelegramNotifier)
    assert notifier._token == "1:TOK"
    assert notifier._chat_id == "42"
    assert notifier._proxies == "http://127.0.0.1:20172"


def test_empty_notifications_disables_notifier():
    assert build_notifier(_config([])) is None


def test_multiple_entries_use_first_with_warning():
    config = _config([
        {"type": "telegram", "token": "1:FIRST", "chat_id": "42"},
        {"type": "telegram", "token": "2:SECOND", "chat_id": "43"},
    ])

    with capture_logs() as get_logs:
        notifier = build_notifier(config)
        logs = get_logs()

    assert isinstance(notifier, TelegramNotifier)
    assert notifier._token == "1:FIRST"
    assert "only the first Telegram entry is used" in logs
    assert "Multiple notification entries configured (2)" in logs


def test_legacy_webhook_entry_fails_clearly():
    with pytest.raises(ValidationError) as exc_info:
        _config([{"type": "webhook", "url": "http://x"}])

    message = str(exc_info.value)
    assert "webhook" in message
    assert "webhook 通知后端已移除" in message


def test_unknown_url_field_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        _config([{"type": "telegram", "url": "http://x"}])

    message = str(exc_info.value)
    assert "url" in message
    assert "Extra inputs are not permitted" in message
