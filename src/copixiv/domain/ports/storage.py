"""Storage ports — file and image download abstractions."""

from pathlib import Path
from typing import Protocol

from copixiv.domain.models.novel import Novel


class FileStoragePort(Protocol):
    """Port for reading and writing downloaded novel files."""

    download_dir: str

    def novel_text_path(self, novel_id: int, title: str) -> Path: ...
    def novel_epub_path(self, novel_id: int, title: str) -> Path: ...
    def save_novel_text(
        self, novel_id: int, title: str, content: str, force: bool = False
    ) -> Path: ...
    def delete_novel_files(self, novel_path: str) -> None: ...


class ImageDownloaderPort(Protocol):
    """Port for downloading images (covers, illustrations).

    Asset processing takes the domain :class:`Novel` — typed contract, no
    raw dicts (docs/MODULARITY.md §M5).
    """

    async def download_image(self, url: str, save_path: Path) -> bool: ...
    async def process_novel_assets(
        self, novel: Novel, force: bool = False
    ) -> None: ...
    async def await_all(self) -> list[tuple[int, str]]: ...
    def shutdown(self) -> None: ...
