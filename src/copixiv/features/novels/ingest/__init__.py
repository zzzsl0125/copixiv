"""Novel ingestion pipeline — network fetch, asset downloads, and persistence."""

from .asset_pipeline import AssetBatch, NovelAssetPipeline
from .download import fetch_novel_and_assets
from .persist import persist_novels
from .pipeline import IngestOutcome, ingest

__all__ = [
    "AssetBatch",
    "IngestOutcome",
    "NovelAssetPipeline",
    "fetch_novel_and_assets",
    "ingest",
    "persist_novels",
]
