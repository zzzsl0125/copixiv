"""EPUB builder port."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class EpubBuilderPort(Protocol):
    """Port for creating EPUB files from downloaded novel text and images."""

    def create_epub(
        self, data: dict, compress_quality: int = 75
    ) -> bool: ...
