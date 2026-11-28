"""Novels sub-routers — static/download routes registered before ``/{novel_id}``."""

from . import batch, downloads, novel, query, series_actions

ROUTERS = (
    query.router,
    batch.router,
    downloads.router,
    series_actions.router,
    novel.router,
)

__all__ = ["ROUTERS"]
