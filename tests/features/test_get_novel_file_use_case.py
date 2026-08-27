"""Unit tests for GetNovelFileUseCase (file serving + path-escape guard)."""

import pytest

from copixiv.features.novels.get_novel_file import GetNovelFileUseCase
from copixiv.core.exceptions import NotFoundError


class _FakeNovelRepo:
    def __init__(self, novel):
        self._novel = novel

    async def get_by_id(self, novel_id):
        if novel_id != self._novel.id:
            return None
        return self._novel


def _novel(novel_id: int, path: str | None):
    from types import SimpleNamespace
    return SimpleNamespace(id=novel_id, path=path)


async def test_returns_resolved_path_and_txt_media_type(tmp_path):
    target = tmp_path / "novel.txt"
    target.write_text("正文", encoding="utf-8")
    use_case = GetNovelFileUseCase(
        _FakeNovelRepo(_novel(1, str(target))), download_root=str(tmp_path),
    )

    path, media_type = await use_case.execute(1, "txt")

    assert path == target.resolve()
    assert media_type == "text/plain"


async def test_epub_format_switches_extension_and_media_type(tmp_path):
    target = tmp_path / "novel.txt"
    target.write_text("正文", encoding="utf-8")
    (tmp_path / "novel.epub").write_bytes(b"epub")
    use_case = GetNovelFileUseCase(
        _FakeNovelRepo(_novel(1, str(target))), download_root=str(tmp_path),
    )

    path, media_type = await use_case.execute(1, "epub")

    assert path.suffix == ".epub"
    assert media_type == "application/epub+zip"


async def test_unknown_novel_raises_not_found(tmp_path):
    use_case = GetNovelFileUseCase(
        _FakeNovelRepo(_novel(1, str(tmp_path / "x.txt"))),
        download_root=str(tmp_path),
    )
    with pytest.raises(NotFoundError):
        await use_case.execute(999, "txt")


async def test_missing_path_raises_not_found(tmp_path):
    use_case = GetNovelFileUseCase(
        _FakeNovelRepo(_novel(1, None)), download_root=str(tmp_path),
    )
    with pytest.raises(NotFoundError, match="no file path"):
        await use_case.execute(1, "txt")


async def test_file_not_on_disk_raises_not_found(tmp_path):
    use_case = GetNovelFileUseCase(
        _FakeNovelRepo(_novel(1, str(tmp_path / "missing.txt"))),
        download_root=str(tmp_path),
    )
    with pytest.raises(NotFoundError, match="File not found"):
        await use_case.execute(1, "txt")


async def test_path_escaping_download_root_raises_not_found(tmp_path):
    use_case = GetNovelFileUseCase(
        _FakeNovelRepo(_novel(1, "/etc/passwd")),
        download_root=str(tmp_path),
    )
    with pytest.raises(NotFoundError, match="escapes"):
        await use_case.execute(1, "txt")
