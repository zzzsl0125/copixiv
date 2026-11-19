"""Novels sub-routers — static/download routes registered before ``/{novel_id}``."""

from . import batch, downloads, novel, query

ROUTERS = (
    query.router,
    batch.router,
    downloads.router,
    novel.router,
)

__all__ = ["ROUTERS"]
