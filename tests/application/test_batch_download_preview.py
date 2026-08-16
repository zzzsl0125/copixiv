"""Tests for BatchDownloadUseCase.preview — naming-template preview."""

import pytest

from copixiv.application.novel.batch_download import BatchDownloadUseCase
from copixiv.domain.exceptions import ValidationError


class _FakeNovelRepo:
    def __init__(self, novels):
        self._novels = novels

    async def get_novels(self, **kwargs):
        per_page = kwargs.get("per_page", 20)
        return {"novels": self._novels[:per_page], "cursor": None}


def _novel(**overrides):
    base = {
        "id": 123,
        "title": "测试标题",
        "author_name": "作者A",
        "author_id": 456,
        "like": 10,
        "view": 100,
        "text": 5000,
        "create_time": "2024-01-02T03:04:05",
        "series_name": "系列B",
        "series_index": 2,
        "has_epub": 0,
    }
    base.update(overrides)
    return base


async def test_preview_resolves_first_novel():
    use_case = BatchDownloadUseCase(_FakeNovelRepo([_novel()]))
    path = await use_case.preview(naming_template="{author_name}/{title}_{id}")
    assert path == "作者A/测试标题_123.txt"


async def test_preview_prefers_epub_when_available():
    use_case = BatchDownloadUseCase(_FakeNovelRepo([_novel(has_epub=2)]))
    path = await use_case.preview(
        naming_template="{title}_{id}", format_mode="prefer_epub"
    )
    assert path == "测试标题_123.epub"


async def test_preview_returns_none_when_no_match():
    use_case = BatchDownloadUseCase(_FakeNovelRepo([]))
    path = await use_case.preview(naming_template="{id}")
    assert path is None


async def test_preview_rejects_template_without_id():
    use_case = BatchDownloadUseCase(_FakeNovelRepo([_novel()]))
    with pytest.raises(ValidationError, match="must contain '\\{id\\}'"):
        await use_case.preview(naming_template="{title}")


async def test_preview_uses_configured_default_template():
    use_case = BatchDownloadUseCase(
        _FakeNovelRepo([_novel()]), naming_template="{id}_{title}"
    )
    path = await use_case.preview()
    assert path == "123_测试标题.txt"
