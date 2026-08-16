"""Novel repository — read/write split facade (docs/MODULARITY.md §M4).

Read queries live in ``novel_read.py``; write operations in
``novel_write.py``.  This module keeps the single
``SQLAlchemyNovelRepository`` entry point that ``SqlUnitOfWork`` exposes,
so consumers are unchanged.
"""

from .novel_read import SQLAlchemyNovelReadRepository
from .novel_write import SQLAlchemyNovelWriteRepository


class SQLAlchemyNovelRepository(
    SQLAlchemyNovelReadRepository,
    SQLAlchemyNovelWriteRepository,
):
    """Facade: read + write halves of the novel repository."""
