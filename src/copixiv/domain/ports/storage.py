"""Storage ports — file and image download abstractions."""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class FileStoragePort(Protocol):
    """Port for reading and writing downloaded novel files."""

    def novel_text_path(self, novel_id: int, title: str) -> Path: ...
    def novel_epub_path(self, novel_id: int, title: str) -> Path: ...
    def save_novel_text(
        self, novel_id: int, title: str, content: str, force: bool = False
    ) -> Path: ...
    def delete_novel_files(self, novel_path: str) -> None: ...


@runtime_checkable
class ImageDownloaderPort(Protocol):
    """Port for downloading images (covers, illustrations)."""

    async def download_image(self, url: str, save_path: Path) -> bool: ...
    async def process_novel_assets(
        self, data: dict, force: bool = False
    ) -> None: ...
    def shutdown(self) -> None: ...
