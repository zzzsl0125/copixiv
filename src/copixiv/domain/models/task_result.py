"""TaskResult — structured return value for all background tasks.

Replaces the ad-hoc ``list[str] | int | None`` returns so that:
* Novel-discovery tasks can clearly mark which titles are new.
* Maintenance tasks can report a plain summary without polluting the
  ``new_novel_titles`` column.
* The notifier can decide *how* to format the message based on whether
  the task actually discovered novels.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class TaskResult(BaseModel):
    """Structured result from a task execution.

    Attributes:
        summary: Human-readable one-line summary used in notifications and
            log output.  Always populated.
        new_novel_titles: Titles of novels that were **newly discovered and
            persisted** during this task run.  Only populated by tasks that
            actually fetch/download novels (e.g. ``novel_follow``,
            ``author_fetch``).  Maintenance tasks leave this empty.
        new_novel_count: Total count of newly persisted novels.  Always
            mirrors ``len(new_novel_titles)`` (enforced by a validator), so
            callers never need to set it explicitly.
    """

    summary: str = ""
    new_novel_titles: list[str] = Field(default_factory=list)
    new_novel_count: int = 0

    @model_validator(mode="after")
    def _sync_count(self) -> "TaskResult":
        if self.new_novel_titles:
            self.new_novel_count = len(self.new_novel_titles)
        else:
            self.new_novel_count = 0
        return self
