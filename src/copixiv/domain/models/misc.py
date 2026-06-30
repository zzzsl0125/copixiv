"""Miscellaneous domain entities."""

from pydantic import BaseModel


class FailedNovel(BaseModel):
    """Record of a novel that failed to download."""

    novel_id: int
    failure_type: str | None = None
    error_message: str | None = None
    failed_times: int = 1


class ProcessedPeriod(BaseModel):
    """Marks a time period as already processed (day/month/year)."""

    period_type: str  # 'day', 'month', 'year'
    period_value: str  # YYYY-MM-DD etc


class NovelEpubConversion(BaseModel):
    """Tracks EPUB conversion status for a novel."""

    novel_id: int
    status: str = "pending"
    last_processed: str | None = None


