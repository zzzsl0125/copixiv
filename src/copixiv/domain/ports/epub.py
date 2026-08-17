"""EPUB builder port."""

from typing import Protocol, runtime_checkable

from copixiv.domain.models.novel import Novel


@runtime_checkable
class EpubBuilderPort(Protocol):
    """Port for creating EPUB files from downloaded novel text and images.

    The input is the domain :class:`Novel` — typed contract, no raw dicts
    (docs/MODULARITY.md §M5).
    """

    def create_epub(
        self, novel: Novel, compress_quality: int = 75
    ) -> bool: ...
